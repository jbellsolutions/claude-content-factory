#!/usr/bin/env python3
from __future__ import annotations

import cgi
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from factory_ingest import INBOX, JOBS, create_job_from_folder, load_env_config, run_job, slugify
from runtime_paths import CODE_ROOT, DATA_ROOT, ensure_runtime_dirs

ROOT = CODE_ROOT
STATE_FILE = DATA_ROOT / ".dashboard_state.json"
QUEUE_FILE = DATA_ROOT / ".posting_queue.json"
STATE_LOCK = threading.Lock()
QUEUE_LOCK = threading.Lock()
DEFAULT_POST_CHANNELS = [
    "newsletter",
    "facebook_post",
    "linkedin_post",
    "linkedin_article",
    "medium_article",
    "substack_post",
    "youtube_package",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"jobs": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"jobs": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {"jobs": {}}
    try:
        return json.loads(QUEUE_FILE.read_text())
    except json.JSONDecodeError:
        return {"jobs": {}}


def save_queue(queue: dict) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def queue_record(slug: str) -> dict:
    return load_queue().get("jobs", {}).get(slug, {})


def queue_job_for_local_post(slug: str, requested_channels: list[str] | None = None) -> dict:
    with QUEUE_LOCK:
        queue = load_queue()
        jobs = queue.setdefault("jobs", {})
        current = jobs.get(slug, {})
        current.update(
            {
                "slug": slug,
                "status": "pending",
                "requested_at": now_iso(),
                "claimed_at": "",
                "completed_at": "",
                "worker_id": "",
                "error": "",
                "requested_channels": requested_channels or DEFAULT_POST_CHANNELS,
            }
        )
        jobs[slug] = current
        save_queue(queue)
        return current


def claim_next_queue_item(worker_id: str) -> dict | None:
    with QUEUE_LOCK:
        queue = load_queue()
        jobs = queue.setdefault("jobs", {})
        pending = [
            item for item in jobs.values()
            if item.get("status") == "pending"
        ]
        pending.sort(key=lambda item: (item.get("requested_at", ""), item.get("slug", "")))
        if not pending:
            return None
        item = pending[0]
        slug = item["slug"]
        item["status"] = "claimed"
        item["claimed_at"] = now_iso()
        item["worker_id"] = worker_id
        item["error"] = ""
        jobs[slug] = item
        save_queue(queue)
        return item


def complete_queue_item(slug: str, worker_id: str, results: dict) -> None:
    with QUEUE_LOCK:
        queue = load_queue()
        jobs = queue.setdefault("jobs", {})
        current = jobs.get(slug, {"slug": slug})
        current.update(
            {
                "status": "completed",
                "completed_at": now_iso(),
                "worker_id": worker_id,
                "error": "",
                "results": results,
            }
        )
        jobs[slug] = current
        save_queue(queue)
    output_path = JOBS / slug / "output" / "content_pack" / "distribution-results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    update_job_state(slug, status="autoposted", distribution_summary="Local posting worker completed.", distribution_error="")


def fail_queue_item(slug: str, worker_id: str, error: str) -> None:
    with QUEUE_LOCK:
        queue = load_queue()
        jobs = queue.setdefault("jobs", {})
        current = jobs.get(slug, {"slug": slug})
        current.update(
            {
                "status": "failed",
                "completed_at": now_iso(),
                "worker_id": worker_id,
                "error": error,
            }
        )
        jobs[slug] = current
        save_queue(queue)
    fallback_status = "completed" if (JOBS / slug / "output" / "index.html").exists() else "failed"
    update_job_state(slug, status=fallback_status, distribution_summary="", distribution_error=error)


def update_job_state(slug: str, **changes: object) -> None:
    with STATE_LOCK:
        state = load_state()
        jobs = state.setdefault("jobs", {})
        current = jobs.get(slug, {})
        current.update(changes)
        current["updated_at"] = now_iso()
        jobs[slug] = current
        save_state(state)


def remove_job_state(slug: str) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("jobs", {}).pop(slug, None)
        save_state(state)


def all_jobs() -> list[dict]:
    state = load_state()
    queue = load_queue().get("jobs", {})
    items: list[dict] = []
    for slug, record in state.get("jobs", {}).items():
        record = {"slug": slug, **record}
        job_dir = JOBS / slug
        manifest_path = job_dir / "job.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            record.setdefault("title", manifest.get("title", slug))
        else:
            record.setdefault("title", slug.replace("-", " ").title())
        output_dir = job_dir / "output"
        record["has_output"] = output_dir.exists() and (output_dir / "index.html").exists()
        if slug in queue:
            record["posting_queue"] = queue[slug]
        items.append(record)
    for manifest_path in sorted(JOBS.glob("*/job.json")):
        slug = manifest_path.parent.name
        if any(item["slug"] == slug for item in items):
            continue
        manifest = json.loads(manifest_path.read_text())
        output_dir = manifest_path.parent / "output"
        items.append(
            {
                "slug": slug,
                "title": manifest.get("title", slug.replace("-", " ").title()),
                "status": "completed" if (output_dir / "index.html").exists() else "unknown",
                "updated_at": now_iso(),
                "has_output": (output_dir / "index.html").exists(),
                "posting_queue": queue.get(slug, {}),
            }
        )
    items.sort(key=lambda item: (item.get("updated_at", ""), item["slug"]), reverse=True)
    return items


def job_record(slug: str) -> dict:
    state = load_state().get("jobs", {}).get(slug, {})
    posting_queue = queue_record(slug)
    job_dir = JOBS / slug
    manifest_path = job_dir / "job.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    output_dir = job_dir / "output"
    return {
        "slug": slug,
        "title": manifest.get("title", slug.replace("-", " ").title()),
        "status": state.get("status", "unknown"),
        "updated_at": state.get("updated_at", ""),
        "site_url": state.get("site_url", ""),
        "repo_name": state.get("repo_name", ""),
        "error": state.get("error", ""),
        "publish_error": state.get("publish_error", ""),
        "distribution_error": state.get("distribution_error", ""),
        "has_output": (output_dir / "index.html").exists(),
        "has_content_pack": (output_dir / "content_pack" / "README.md").exists(),
        "posting_queue": posting_queue,
    }


def infer_output_url(slug: str) -> str:
    return f"/preview/{slug}/index.html"


def infer_run_url(slug: str) -> str:
    return f"/run/{slug}"


def safe_relative_path(base: Path, raw: str) -> Path | None:
    candidate = (base / urllib.parse.unquote(raw.lstrip("/"))).resolve()
    try:
        candidate.relative_to(base.resolve())
        return candidate
    except ValueError:
        return None


def save_upload(field: cgi.FieldStorage, destination: Path) -> bool:
    file_obj = getattr(field, "file", None)
    if file_obj is None:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    return destination.stat().st_size > 0


def build_folder_name(title: str, filename: str) -> str:
    stem = slugify(title) or slugify(Path(filename).stem) or "upload"
    return f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def format_subprocess_error(exc: subprocess.CalledProcessError) -> str:
    parts = [f"Command failed with exit status {exc.returncode}:"]
    if exc.cmd:
        parts.append(" ".join(str(item) for item in exc.cmd))
    stdout = (exc.stdout or "").strip()
    stderr = (exc.stderr or "").strip()
    if stdout:
        parts.extend(["", "STDOUT:", stdout])
    if stderr:
        parts.extend(["", "STDERR:", stderr])
    return "\n".join(parts).strip()


def preferred_distribution_python() -> str:
    env = load_env_config()
    configured = env.get("BROWSER_USE_PYTHON", "").strip()
    if configured and Path(configured).exists():
        return configured
    candidates = [
        Path.home() / ".browser-use-env" / "bin" / "python",
        ROOT / ".browser-use-env" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def dashboard_html(message: str = "") -> str:
    env = load_env_config()
    rows = []
    for job in all_jobs():
        slug = job["slug"]
        title = job.get("title", slug.replace("-", " ").title())
        status = job.get("status", "unknown")
        updated_at = job.get("updated_at", "")
        repo_name = job.get("repo_name", "")
        site_url = job.get("site_url", "")
        error = job.get("error", "")
        publish_error = job.get("publish_error", "")
        posting_queue = job.get("posting_queue", {})
        posting_status = posting_queue.get("status", "")
        preview = infer_output_url(slug) if job.get("has_output") else ""
        run_url = infer_run_url(slug)
        run_link = f'<a class="ghost" href="{run_url}">Open Run</a>'
        preview_link = f'<a class="ghost" href="{preview}">Preview</a>' if preview else ""
        content_pack = f"/preview/{slug}/content_pack/README.md" if (JOBS / slug / "output" / "content_pack" / "README.md").exists() else ""
        content_link = f'<a class="ghost" href="{content_pack}">Content Pack</a>' if content_pack else ""
        live_link = f'<a class="ghost" href="{site_url}" target="_blank" rel="noreferrer">Live Site</a>' if site_url else ""
        publish_form = ""
        delete_form = f"""
        <form class="delete-form" method="post" action="/delete" onsubmit="return confirm('Delete this run from the dashboard and local storage?');">
          <input type="hidden" name="slug" value="{slug}" />
          <button type="submit" class="danger-button">Delete</button>
        </form>
        """
        rerun_form = ""
        if (JOBS / slug / "job.json").exists():
            rerun_form = f"""
            <form class="inline-form" method="post" action="/rerun">
              <input type="hidden" name="slug" value="{slug}" />
              <button type="submit">Rerun</button>
            </form>
            """
        autopost_form = ""
        queue_form = ""
        if job.get("has_output"):
            autopost_form = f"""
            <form class="inline-form" method="post" action="/autopost">
              <input type="hidden" name="slug" value="{slug}" />
              <button type="submit">Approve &amp; Post</button>
            </form>
            """
            queue_form = f"""
            <form class="inline-form" method="post" action="/queue-post">
              <input type="hidden" name="slug" value="{slug}" />
              <button type="submit">Queue Local Post</button>
            </form>
            """
        if job.get("has_output") and status not in {"publishing", "published"}:
            publish_form = f"""
            <form class="inline-form" method="post" action="/publish">
              <input type="hidden" name="slug" value="{slug}" />
              <input type="text" name="repo_name" value="{repo_name or slug}" />
              <button type="submit">Publish</button>
            </form>
            """
        error_block = f'<p class="error">{error}</p>' if error else ""
        publish_warning_block = f'<p class="warning"><strong>Publish warning:</strong>\n{publish_error}</p>' if publish_error else ""
        queue_block = f'<p class="queue-note"><strong>Local posting queue:</strong> {posting_status}</p>' if posting_status else ""
        rows.append(
            f"""
            <article class="job-card">
              <div class="job-head">
                <div>
                  <p class="eyebrow">Job</p>
                  <h3>{title}</h3>
                </div>
                <span class="status status-{status}">{status}</span>
              </div>
              <p class="meta"><strong>Slug:</strong> {slug}</p>
              <p class="meta"><strong>Updated:</strong> {updated_at}</p>
              <div class="job-actions">
                {run_link}
                {preview_link}
                {content_link}
                {live_link}
                {rerun_form}
                {autopost_form}
                {queue_form}
                {publish_form}
                {delete_form}
              </div>
              {queue_block}
              {error_block}
              {publish_warning_block}
            </article>
            """
        )
    jobs_markup = "\n".join(rows) if rows else '<p class="empty">No jobs yet. Upload a video to create one.</p>'
    info_box = f'<div class="flash">{message}</div>' if message else ""
    default_cta = env.get("DEFAULT_CTA_URL", "https://jbellsolutions.github.io/claude-code-ecosystem-certification/")
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Claude Content Factory Dashboard</title>
    <style>
      :root{{--surface:rgba(248,239,225,.88);--surface-2:rgba(255,248,239,.68);--ink:#12313e;--muted:#53686e;--line:rgba(18,49,62,.14);--accent:#ea7f3a;--green:#2f7456;--amber:#9b5c18;--red:#8d2f2f;--shadow:0 30px 80px rgba(4,20,29,.24)}}
      *{{box-sizing:border-box}}body{{margin:0;font-family:"Avenir Next","Segoe UI",sans-serif;color:var(--ink);background:radial-gradient(circle at top left,rgba(234,127,58,.28),transparent 24%),radial-gradient(circle at top right,rgba(85,131,141,.22),transparent 24%),linear-gradient(180deg,#d2b28d 0%,#173441 42%,#081721 100%);min-height:100vh}}
      .shell{{width:min(1220px,calc(100vw - 28px));margin:0 auto;padding:24px 0 42px}}
      .topbar,.panel,.jobs,.tabs-shell{{margin-top:24px;border-radius:32px;background:var(--surface);border:1px solid rgba(255,255,255,.34);box-shadow:var(--shadow);backdrop-filter:blur(18px)}}
      .topbar{{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:20px 24px}}
      .eyebrow{{margin:0 0 8px;text-transform:uppercase;letter-spacing:.08em;font-size:.76rem;font-weight:800;color:var(--accent)}}
      h1,h2,h3{{margin:0;font-family:"New York","Iowan Old Style",Georgia,serif}} h1{{font-size:clamp(2.6rem,5vw,4.4rem);line-height:.95}} h2{{font-size:clamp(2rem,4vw,3rem);line-height:.98}} h3{{font-size:1.5rem;line-height:1.05}}
      p{{margin:0}} .muted{{color:var(--muted);line-height:1.65;max-width:56rem}}
      .flash{{margin-top:18px;padding:14px 18px;border-radius:18px;background:rgba(47,116,86,.12);border:1px solid rgba(47,116,86,.18);color:var(--green)}}
      .tabs-shell{{padding:14px 18px}} .dashboard-tabs{{display:flex;gap:10px;flex-wrap:wrap}} .dashboard-tab{{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 18px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.56);color:var(--ink);text-decoration:none;font-weight:800}} .dashboard-tab.active{{background:var(--ink);color:#fff8ef;border-color:transparent}}
      .dashboard-section.hidden{{display:none}}
      .panel{{display:grid;grid-template-columns:1.1fr .9fr;gap:24px;padding:28px}}
      .form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
      .field,.field-full{{display:flex;flex-direction:column;gap:8px}}
      .field-full{{grid-column:1 / -1}}
      label{{font-size:.92rem;font-weight:700}}
      input[type=text],input[type=url],textarea,input[type=file]{{width:100%;padding:14px 16px;border-radius:18px;border:1px solid var(--line);background:rgba(255,255,255,.76);font:inherit;color:var(--ink)}}
      textarea{{min-height:110px;resize:vertical}}
      .checkline{{display:flex;align-items:center;gap:10px;margin-top:10px;font-weight:600}}
      button,.ghost{{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:0 20px;border-radius:999px;border:0;font-weight:800;font:inherit;text-decoration:none}}
      button{{background:var(--ink);color:#fff8ef;cursor:pointer}} .ghost{{background:rgba(255,255,255,.62);color:var(--ink);border:1px solid var(--line)}}
      .actions{{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}}
      .jobs{{padding:28px}}
      .job-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:22px}}
      .job-card{{padding:16px;border-radius:22px;background:var(--surface-2);border:1px solid rgba(255,255,255,.34)}}
      .job-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}
      .job-head h3{{font-size:1.18rem;line-height:1.08}}
      .status{{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:0 12px;border-radius:999px;font-size:.85rem;font-weight:800;text-transform:capitalize}}
      .status-queued,.status-publishing,.status-autoposting{{background:rgba(155,92,24,.12);color:var(--amber)}}
      .status-running{{background:rgba(21,53,64,.12);color:var(--ink)}}
      .status-completed,.status-published,.status-autoposted{{background:rgba(47,116,86,.12);color:var(--green)}}
      .status-failed{{background:rgba(141,47,47,.12);color:var(--red)}}
      .meta{{margin-top:8px;color:var(--muted);font-size:.95rem}}
      .job-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
      .job-actions .ghost,.job-actions button{{min-height:40px;padding:0 14px;font-size:.92rem}}
      .inline-form{{display:flex;gap:8px;flex-wrap:wrap}} .inline-form input{{width:180px;padding:10px 12px;border-radius:999px}}
      .delete-form{{display:inline-flex}} .danger-button{{background:rgba(141,47,47,.12);color:var(--red);border:1px solid rgba(141,47,47,.18)}}
      .empty{{margin-top:18px;color:var(--muted)}}
      .error{{margin-top:14px;padding:12px 14px;border-radius:16px;background:rgba(141,47,47,.08);color:var(--red);white-space:pre-wrap}}
      .warning{{margin-top:14px;padding:12px 14px;border-radius:16px;background:rgba(234,127,58,.12);color:#8f4d18;white-space:pre-wrap}}
      .queue-note{{margin-top:14px;padding:12px 14px;border-radius:16px;background:rgba(18,49,62,.06);color:var(--ink)}}
      .tips{{padding:22px;border-radius:26px;background:linear-gradient(180deg,rgba(8,23,33,.98),rgba(14,36,48,.92));color:#f8efe1}}
      .tips p,.tips li{{color:rgba(248,239,225,.84);line-height:1.6}} .tips ul{{margin:16px 0 0;padding-left:20px}}
      .lightbox{{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(8,23,33,.68);backdrop-filter:blur(8px);z-index:1000}}
      .lightbox.active{{display:flex}}
      .lightbox-card{{width:min(560px,calc(100vw - 32px));padding:28px;border-radius:28px;background:rgba(248,239,225,.95);box-shadow:var(--shadow)}}
      .loader{{height:12px;margin-top:18px;border-radius:999px;background:rgba(18,49,62,.1);overflow:hidden}}
      .loader span{{display:block;height:100%;width:12%;border-radius:999px;background:linear-gradient(90deg,var(--accent),#f7c38a,var(--accent));transition:width .2s ease}}
      .lightbox-status{{margin-top:14px;color:var(--muted);font-weight:700}}
      @keyframes slide{{0%{{transform:translateX(-120%)}}100%{{transform:translateX(320%)}}}}
      @media (max-width:980px){{.panel{{grid-template-columns:1fr}}.form-grid{{grid-template-columns:1fr}}}}
    </style>
  </head>
  <body>
    <div class="shell">
      <header class="topbar">
        <div>
          <p class="eyebrow">Dashboard</p>
          <h1>Claude Content Factory</h1>
          <p class="muted">Upload a source video, optional audio and VTT, then let the same pipeline build the edited video, landing page, PDF, and publish-ready output.</p>
        </div>
      </header>
      {info_box}
      <section class="tabs-shell">
        <nav class="dashboard-tabs">
          <a class="dashboard-tab active" href="#create" data-tab="create">Create</a>
          <a class="dashboard-tab" href="#runs" data-tab="runs">Runs</a>
        </nav>
      </section>
      <section class="panel dashboard-section" id="tab-create">
        <div>
          <p class="eyebrow">New Job</p>
          <h2>Drop in a video and run the pipeline.</h2>
          <form method="post" action="/upload" enctype="multipart/form-data" id="upload-form">
            <div class="form-grid">
              <div class="field"><label for="title">Title</label><input id="title" type="text" name="title" placeholder="Leave blank to infer from transcript" /></div>
              <div class="field"><label for="repo_name">Repo Name</label><input id="repo_name" type="text" name="repo_name" placeholder="claude-in-15-minutes-lead-magnet" /></div>
              <div class="field-full"><label for="headline">Headline</label><input id="headline" type="text" name="headline" placeholder="Leave blank to infer from transcript" /></div>
              <div class="field-full"><label for="subheadline">Subheadline</label><input id="subheadline" type="text" name="subheadline" placeholder="Leave blank to infer from transcript" /></div>
              <div class="field-full"><label for="lead">Lead</label><textarea id="lead" name="lead" placeholder="Leave blank to infer from transcript"></textarea></div>
              <div class="field-full"><label for="checklist">Checklist</label><textarea id="checklist" name="checklist" placeholder="One item per line, or leave blank to infer from transcript"></textarea></div>
              <div class="field"><label for="cta_url">CTA URL</label><input id="cta_url" type="url" name="cta_url" value="{default_cta}" /></div>
              <div class="field"><label for="cta_label">CTA Label</label><input id="cta_label" type="text" name="cta_label" value="Take the full level one certification here for free" /></div>
              <div class="field"><label for="brand_name">Brand Name</label><input id="brand_name" type="text" name="brand_name" placeholder="Optional. Can be inferred from transcript" /></div>
              <div class="field"><label for="target_audience">Target Audience</label><input id="target_audience" type="text" name="target_audience" placeholder="Optional. Can be inferred from transcript" /></div>
              <div class="field-full"><label for="voice_notes">Voice Notes</label><textarea id="voice_notes" name="voice_notes" placeholder="Direct, tactical, founder-led, authority-building, clear, high-agency, and useful. Sound like a real operator. Not salesy, not promotional, not generic."></textarea></div>
              <div class="field-full"><label for="video">Source Video</label><input id="video" type="file" name="source_video" accept=".mp4,.mov,.m4v" required /></div>
              <div class="field"><label for="audio">Optional Audio</label><input id="audio" type="file" name="source_audio" accept=".m4a,.mp3,.wav" /></div>
              <div class="field"><label for="vtt">Optional VTT</label><input id="vtt" type="file" name="source_vtt" accept=".vtt" /></div>
              <div class="field"><label for="txt">Optional Transcript Text</label><input id="txt" type="file" name="source_text" accept=".txt,text/plain" /></div>
            </div>
            <label class="checkline"><input type="checkbox" name="publish_now" value="1" /> Publish to GitHub after build</label>
            <label class="checkline"><input type="checkbox" name="generate_content_pack" value="1" checked /> Generate Facebook, LinkedIn, Medium, Substack, newsletter, and YouTube-ready content</label>
            <div class="actions"><button type="submit">Create Job</button></div>
          </form>
        </div>
        <aside class="tips">
          <p class="eyebrow">How It Works</p>
          <h2>One engine, multiple triggers.</h2>
          <ul>
            <li>The dashboard saves uploads into the same pipeline used by Slack and the drop folder.</li>
            <li>Every job gets its own manifest, edited video, PDF, transcript, and landing page output.</li>
            <li>Use the preview link to review locally before publishing.</li>
            <li>Use the publish control when you want a GitHub repo and Pages site.</li>
          </ul>
        </aside>
      </section>
      <section class="jobs dashboard-section hidden" id="tab-runs">
        <p class="eyebrow">Recent Jobs</p>
        <h2>Track builds, previews, and publishing.</h2>
        <div class="job-grid">
          {jobs_markup}
        </div>
      </section>
    </div>
    <div class="lightbox" id="submit-lightbox" aria-hidden="true">
      <div class="lightbox-card">
        <p class="eyebrow">Processing</p>
        <h2>Uploading and queueing your run.</h2>
        <p class="muted">Keep this page open. You’ll land on the run workspace next, where status will keep updating until the build finishes.</p>
        <div class="loader"><span id="upload-progress-fill"></span></div>
        <p class="lightbox-status" id="upload-progress-text">Starting upload...</p>
      </div>
    </div>
    <script>
      (() => {{
        const form = document.getElementById('upload-form');
        const lightbox = document.getElementById('submit-lightbox');
        const progressFill = document.getElementById('upload-progress-fill');
        const progressText = document.getElementById('upload-progress-text');
        const tabs = Array.from(document.querySelectorAll('[data-tab]'));
        const sections = {{
          create: document.getElementById('tab-create'),
          runs: document.getElementById('tab-runs')
        }};
        function selectTab(name) {{
          tabs.forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
          Object.entries(sections).forEach(([key, el]) => {{
            if (!el) return;
            el.classList.toggle('hidden', key !== name);
          }});
        }}
        const initialTab = window.location.hash === '#runs' ? 'runs' : 'create';
        selectTab(initialTab);
        tabs.forEach((tab) => {{
          tab.addEventListener('click', (event) => {{
            event.preventDefault();
            const next = tab.dataset.tab || 'create';
            window.history.replaceState(null, '', '#' + next);
            selectTab(next);
          }});
        }});
        if (!form || !lightbox) return;
        form.addEventListener('submit', (event) => {{
          event.preventDefault();
          lightbox.classList.add('active');
          lightbox.setAttribute('aria-hidden', 'false');
          const button = form.querySelector('button[type="submit"]');
          if (button) {{
            button.disabled = true;
            button.textContent = 'Submitting...';
          }}
          if (progressFill) progressFill.style.width = '8%';
          if (progressText) progressText.textContent = 'Uploading source files...';
          const xhr = new XMLHttpRequest();
          xhr.open('POST', form.action, true);
          xhr.upload.addEventListener('progress', (progressEvent) => {{
            if (!progressEvent.lengthComputable) return;
            const ratio = Math.max(8, Math.min(92, Math.round((progressEvent.loaded / progressEvent.total) * 92)));
            if (progressFill) progressFill.style.width = ratio + '%';
            if (progressText) progressText.textContent = 'Uploading source files... ' + ratio + '%';
          }});
          xhr.addEventListener('load', () => {{
            if (progressFill) progressFill.style.width = '100%';
            if (xhr.status >= 200 && xhr.status < 400) {{
              if (progressText) progressText.textContent = 'Upload complete. Opening your run...';
              window.location.href = xhr.responseURL || '/#runs';
              return;
            }}
            if (progressText) progressText.textContent = 'Upload failed. Please try again.';
            lightbox.classList.remove('active');
            lightbox.setAttribute('aria-hidden', 'true');
            if (button) {{
              button.disabled = false;
              button.textContent = 'Create Job';
            }}
            alert('Upload failed with status ' + xhr.status + '.');
          }});
          xhr.addEventListener('error', () => {{
            if (progressText) progressText.textContent = 'Upload failed. Please try again.';
            lightbox.classList.remove('active');
            lightbox.setAttribute('aria-hidden', 'true');
            if (button) {{
              button.disabled = false;
              button.textContent = 'Create Job';
            }}
            alert('Upload failed due to a network or server error.');
          }});
          xhr.send(new FormData(form));
        }});
      }})();
    </script>
  </body>
</html>"""


def manifest_from_form(form: cgi.FieldStorage) -> dict:
    checklist = [line.strip() for line in form.getfirst("checklist", "").splitlines() if line.strip()]
    auto_fill_fields = [
        field
        for field in ["title", "headline", "subheadline", "lead", "brand_name", "target_audience"]
        if not form.getfirst(field, "").strip()
    ]
    if not checklist:
        auto_fill_fields.append("checklist")
    manifest = {
        "title": form.getfirst("title", "").strip(),
        "headline": form.getfirst("headline", "").strip(),
        "subheadline": form.getfirst("subheadline", "").strip(),
        "lead": form.getfirst("lead", "").strip(),
        "cta_url": form.getfirst("cta_url", "").strip(),
        "cta_label": form.getfirst("cta_label", "").strip(),
        "brand_name": form.getfirst("brand_name", "").strip(),
        "target_audience": form.getfirst("target_audience", "").strip(),
        "voice_notes": form.getfirst("voice_notes", "").strip(),
        "generate_content_pack": form.getfirst("generate_content_pack", "") == "1",
        "checklist": checklist,
        "auto_fill_fields": auto_fill_fields,
    }
    filtered: dict[str, object] = {}
    for key, value in manifest.items():
        if isinstance(value, bool):
            filtered[key] = value
        elif value:
            filtered[key] = value
    return filtered


def content_file_specs() -> list[tuple[str, str, str]]:
    return [
        ("overview", "Overview", "README.md"),
        ("brief", "Authority Brief", "authority-brief.md"),
        ("facebook", "Facebook Post", "facebook-post.md"),
        ("linkedin-post", "LinkedIn Post", "linkedin-post.md"),
        ("linkedin-article", "LinkedIn Article", "linkedin-article.md"),
        ("medium", "Medium Article", "medium-article.md"),
        ("substack", "Substack Post", "substack-post.md"),
        ("newsletter", "Newsletter", "newsletter.md"),
        ("youtube", "YouTube Package", "youtube-package.md"),
        ("distribution", "Distribution Results", "distribution-results.json"),
        ("context", "Source Context", "source-context.md"),
        ("errors", "Errors", "generation-error.md"),
    ]


def read_optional_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def extract_labeled_section(text: str, label: str) -> str:
    pattern = rf"^{re.escape(label)}:\s*(.*?)(?=^[A-Z][A-Za-z0-9 /&-]*:\s|\Z)"
    match = re.search(pattern, text, flags=re.M | re.S)
    return match.group(1).strip() if match else ""


def display_text_for_content(filename: str, text: str) -> str:
    if not text:
        return ""
    if filename == "facebook-post.md":
        return extract_labeled_section(text, "Ready-To-Post Copy") or text
    if filename == "linkedin-post.md":
        return extract_labeled_section(text, "Recommended Post") or text
    if filename == "linkedin-article.md":
        parts = [
            extract_labeled_section(text, "Title"),
            extract_labeled_section(text, "Subtitle"),
            extract_labeled_section(text, "Article"),
            extract_labeled_section(text, "Closing Reflection"),
        ]
        return "\n\n".join(part for part in parts if part)
    if filename == "medium-article.md":
        parts = [
            extract_labeled_section(text, "Title"),
            extract_labeled_section(text, "Subtitle"),
            extract_labeled_section(text, "Article"),
            extract_labeled_section(text, "Final Takeaway"),
        ]
        return "\n\n".join(part for part in parts if part)
    if filename == "substack-post.md":
        parts = [
            extract_labeled_section(text, "Title"),
            extract_labeled_section(text, "Subtitle"),
            extract_labeled_section(text, "Post"),
            extract_labeled_section(text, "Postscript"),
        ]
        return "\n\n".join(part for part in parts if part)
    if filename == "newsletter.md":
        parts = [
            extract_labeled_section(text, "Recommended Subject"),
            extract_labeled_section(text, "Preview Text"),
            extract_labeled_section(text, "Newsletter Body"),
            extract_labeled_section(text, "Soft CTA"),
        ]
        return "\n\n".join(part for part in parts if part)
    return text


def directory_listing_html(slug: str, relative: str, folder: Path) -> str:
    items = []
    for path in sorted(folder.iterdir()):
        label = path.name + ("/" if path.is_dir() else "")
        href = f"/preview/{slug}/{relative.strip('/') + '/' if relative else ''}{path.name}"
        items.append(f'<li><a href="{href}">{escape(label)}</a></li>')
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Folder Listing</title>
    <style>
      body{{font-family:"Avenir Next","Segoe UI",sans-serif;padding:24px;background:#f7efe4;color:#12313e}}
      a{{color:#12313e}} ul{{line-height:1.9}}
    </style>
  </head>
  <body>
    <h1>{escape(relative or slug)}</h1>
    <ul>{''.join(items)}</ul>
  </body>
</html>"""


def run_detail_html(slug: str) -> str:
    job_dir = JOBS / slug
    manifest_path = job_dir / "job.json"
    if not manifest_path.exists():
        return "<h1>Run not found</h1>"

    manifest = json.loads(manifest_path.read_text())
    output_dir = job_dir / "output"
    content_dir = output_dir / "content_pack"
    transcript_path = output_dir / "transcripts" / "transcript.txt"
    preview_url = infer_output_url(slug) if (output_dir / "index.html").exists() else ""
    state = load_state().get("jobs", {}).get(slug, {})
    site_url = state.get("site_url", "")
    status = state.get("status", "unknown")
    error_text = state.get("error", "")
    publish_error = state.get("publish_error", "")
    distribution_error = state.get("distribution_error", "")
    posting_queue = queue_record(slug)
    posting_status = posting_queue.get("status", "")
    progress_value = {
        "queued": 14,
        "running": 58,
        "publishing": 86,
        "completed": 100,
        "published": 100,
        "failed": 100,
    }.get(status, 8)

    tabs: list[str] = []
    panels: list[str] = []
    available = False
    for tab_id, label, filename in content_file_specs():
        file_path = content_dir / filename
        if not file_path.exists():
            continue
        available = True
        raw_text = read_optional_text(file_path)
        display_text = display_text_for_content(filename, raw_text)
        tabs.append(f'<a class="run-tab" href="#{tab_id}">{label}</a>')
        panels.append(
            f"""
            <section class="run-panel" id="{tab_id}">
              <div class="run-panel-head">
                <h2>{label}</h2>
                <div class="run-panel-actions">
                  <button type="button" class="ghost copy-button" data-copy={json.dumps(display_text)}>Copy</button>
                  <a class="ghost" href="/preview/{slug}/content_pack/{filename}" target="_blank" rel="noreferrer">Open File</a>
                </div>
              </div>
              <div class="content-preview">{escape(display_text)}</div>
              <details class="raw-toggle">
                <summary>Show raw output</summary>
                <textarea readonly>{escape(raw_text)}</textarea>
              </details>
            </section>
            """
        )

    transcript_panel = ""
    transcript_exists = transcript_path.exists()
    if transcript_exists:
        tabs.append('<a class="run-tab" href="#transcript">Transcript</a>')
        transcript_panel = f"""
        <section class="run-panel" id="transcript">
          <div class="run-panel-head">
            <h2>Transcript</h2>
            <a class="ghost" href="/preview/{slug}/transcripts/transcript.txt" target="_blank" rel="noreferrer">Open File</a>
          </div>
          <textarea readonly>{escape(read_optional_text(transcript_path))}</textarea>
        </section>
        """

    asset_links = []
    if preview_url:
        asset_links.append(f'<a class="ghost" href="{preview_url}">Landing Page</a>')
    if (output_dir / "edited_video" / "lead-magnet.mp4").exists():
        asset_links.append(f'<a class="ghost" href="/preview/{slug}/edited_video/lead-magnet.mp4" target="_blank" rel="noreferrer">Edited Video</a>')
    if (output_dir / "deliverables" / "companion-guide.pdf").exists():
        asset_links.append(f'<a class="ghost" href="/preview/{slug}/deliverables/companion-guide.pdf" target="_blank" rel="noreferrer">PDF</a>')
    if (content_dir / "distribution-results.json").exists():
        asset_links.append(f'<a class="ghost" href="/preview/{slug}/content_pack/distribution-results.json" target="_blank" rel="noreferrer">Distribution Results</a>')
    if site_url:
        asset_links.append(f'<a class="ghost" href="{site_url}" target="_blank" rel="noreferrer">Live Site</a>')

    empty_state = ""
    if not available:
        empty_state = """
        <section class="run-panel" id="overview">
          <div class="run-panel-head"><h2>Content Pack</h2></div>
          <p class="muted">This run does not have generated channel content yet. If the transcript exists, set <code>OPENAI_API_KEY</code> and run the job again.</p>
        </section>
        """

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(manifest['title'])} Run</title>
    <style>
      :root{{--surface:rgba(248,239,225,.88);--surface-2:rgba(255,248,239,.68);--ink:#12313e;--muted:#53686e;--line:rgba(18,49,62,.14);--accent:#ea7f3a;--shadow:0 30px 80px rgba(4,20,29,.24)}}
      *{{box-sizing:border-box}} body{{margin:0;font-family:"Avenir Next","Segoe UI",sans-serif;color:var(--ink);background:radial-gradient(circle at top left,rgba(234,127,58,.28),transparent 24%),radial-gradient(circle at top right,rgba(85,131,141,.22),transparent 24%),linear-gradient(180deg,#d2b28d 0%,#173441 42%,#081721 100%);min-height:100vh}}
      .shell{{width:min(1280px,calc(100vw - 28px));margin:0 auto;padding:24px 0 42px}}
      .topbar,.summary,.progress-shell,.run-workspace{{margin-top:24px;border-radius:32px;background:var(--surface);border:1px solid rgba(255,255,255,.34);box-shadow:var(--shadow);backdrop-filter:blur(18px)}}
      .topbar,.summary,.progress-shell,.run-workspace{{padding:24px}}
      h1,h2{{margin:0;font-family:"New York","Iowan Old Style",Georgia,serif}} h1{{font-size:clamp(2.4rem,5vw,4rem);line-height:.95}} h2{{font-size:clamp(1.6rem,3vw,2.4rem)}}
      p{{margin:0}} .eyebrow{{margin:0 0 8px;text-transform:uppercase;letter-spacing:.08em;font-size:.76rem;font-weight:800;color:var(--accent)}} .muted{{color:var(--muted);line-height:1.65}}
      .topbar-links,.summary-links,.run-tabs{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}}
      .ghost{{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 18px;border-radius:999px;background:rgba(255,255,255,.62);color:var(--ink);border:1px solid var(--line);text-decoration:none;font-weight:700}}
      .run-tabs{{position:sticky;top:12px;z-index:5;margin-top:0;padding-bottom:18px;background:linear-gradient(180deg,var(--surface),rgba(248,239,225,.95))}}
      .run-tab{{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:0 16px;border-radius:999px;background:rgba(18,49,62,.08);color:var(--ink);text-decoration:none;font-weight:700;border:1px solid var(--line)}}
      .run-panel{{margin-top:18px;padding:22px;border-radius:26px;background:var(--surface-2);border:1px solid rgba(255,255,255,.34)}}
      .run-panel-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}}
      .run-panel-actions{{display:flex;gap:10px;flex-wrap:wrap}}
      .content-preview{{width:100%;min-height:240px;padding:18px;border-radius:20px;border:1px solid var(--line);background:#fffdf9;color:var(--ink);font:16px/1.7 "Avenir Next","Segoe UI",sans-serif;white-space:pre-wrap}}
      .raw-toggle{{margin-top:14px}}
      .raw-toggle summary{{cursor:pointer;font-weight:700;color:var(--muted)}}
      textarea{{width:100%;min-height:520px;padding:18px;border-radius:20px;border:1px solid var(--line);background:#fffdf9;color:var(--ink);font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}}
      .meta-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:18px}}
      .meta-card{{padding:16px;border-radius:20px;background:rgba(255,255,255,.48);border:1px solid var(--line)}} .meta-card strong{{display:block;margin-bottom:6px}}
      .progress-bar{{height:14px;margin-top:18px;border-radius:999px;background:rgba(18,49,62,.1);overflow:hidden}}
      .progress-fill{{height:100%;width:{progress_value}%;border-radius:999px;background:linear-gradient(90deg,var(--accent),#f7c38a);transition:width .5s ease}}
      .status-pill{{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:0 14px;border-radius:999px;background:rgba(18,49,62,.08);font-weight:800;text-transform:capitalize}}
      .status-failed{{background:rgba(141,47,47,.12);color:#8d2f2f}}
      .status-completed,.status-published{{background:rgba(47,116,86,.12);color:#2f7456}}
      .danger-button{{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 18px;border-radius:999px;background:rgba(141,47,47,.12);color:#8d2f2f;border:1px solid rgba(141,47,47,.18);font-weight:800;cursor:pointer}}
      .message-block{{margin-top:18px;padding:14px 16px;border-radius:18px;white-space:pre-wrap;line-height:1.55}}
      .message-error{{background:rgba(141,47,47,.08);color:#8d2f2f}}
      .message-warning{{background:rgba(234,127,58,.12);color:#8f4d18}}
    </style>
  </head>
  <body>
    <div class="shell">
      <header class="topbar">
        <p class="eyebrow">Run Workspace</p>
        <h1>{escape(manifest['title'])}</h1>
        <p class="muted">{escape(manifest.get('headline', ''))}</p>
        <div class="topbar-links">
          <a class="ghost" href="/">Back To Dashboard</a>
          {''.join(asset_links)}
          <form method="post" action="/autopost">
            <input type="hidden" name="slug" value="{escape(slug)}" />
            <button type="submit" class="ghost">Approve &amp; Post</button>
          </form>
          <form method="post" action="/queue-post">
            <input type="hidden" name="slug" value="{escape(slug)}" />
            <button type="submit" class="ghost">Queue Local Post</button>
          </form>
          <form method="post" action="/rerun">
            <input type="hidden" name="slug" value="{escape(slug)}" />
            <button type="submit" class="ghost">Rerun Job</button>
          </form>
          <form method="post" action="/delete" onsubmit="return confirm('Delete this run from the dashboard and local storage?');">
            <input type="hidden" name="slug" value="{escape(slug)}" />
            <button type="submit" class="danger-button">Delete Run</button>
          </form>
        </div>
      </header>
      <section class="progress-shell">
        <p class="eyebrow">Run Status</p>
        <h2 id="status-title">Current status: <span class="status-pill status-{escape(status)}" id="status-pill">{escape(status)}</span></h2>
        <p class="muted" id="status-copy">This page updates while the run is in progress. When the build completes, the generated content and assets stay available here.</p>
        <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
        {'<div class="message-block message-warning" id="posting-queue">Local posting queue: ' + escape(posting_status) + '</div>' if posting_status else ''}
        {'<div class="message-block message-warning" id="publish-warning">' + escape(publish_error) + '</div>' if publish_error else ''}
        {'<div class="message-block message-warning" id="distribution-warning">' + escape(distribution_error) + '</div>' if distribution_error else ''}
        {'<div class="message-block message-error" id="run-error">' + escape(error_text) + '</div>' if error_text else ''}
      </section>
      <section class="summary">
        <p class="eyebrow">Run Summary</p>
        <div class="meta-grid">
          <div class="meta-card"><strong>Slug</strong>{escape(slug)}</div>
          <div class="meta-card"><strong>Status</strong>{escape(state.get('status', 'unknown'))}</div>
          <div class="meta-card"><strong>Updated</strong>{escape(state.get('updated_at', ''))}</div>
          <div class="meta-card"><strong>Content Folder</strong><a href="/preview/{slug}/content_pack" class="ghost">Open Folder</a></div>
        </div>
      </section>
      <section class="run-workspace">
        <div class="run-tabs">
          {''.join(tabs) if tabs else '<span class="muted">No channel content yet.</span>'}
        </div>
        {empty_state}
        {''.join(panels)}
        {transcript_panel}
      </section>
    </div>
    <script>
      (() => {{
        const slug = {json.dumps(slug)};
        const progressFill = document.getElementById('progress-fill');
        const statusPill = document.getElementById('status-pill');
        const statusCopy = document.getElementById('status-copy');
        document.querySelectorAll('.copy-button').forEach((button) => {{
          button.addEventListener('click', async () => {{
            const text = button.dataset.copy || '';
            if (!text) return;
            try {{
              await navigator.clipboard.writeText(text);
              const original = button.textContent;
              button.textContent = 'Copied';
              window.setTimeout(() => {{ button.textContent = original; }}, 1400);
            }} catch (_error) {{
              alert('Copy failed.');
            }}
          }});
        }});
        const values = {{ queued: 14, running: 58, publishing: 86, autoposting: 92, completed: 100, published: 100, autoposted: 100, failed: 100 }};
        function applyStatus(data) {{
          const status = data.status || 'unknown';
          if (progressFill) progressFill.style.width = (values[status] || 8) + '%';
          if (statusPill) {{
            statusPill.textContent = status;
            statusPill.className = 'status-pill status-' + status;
          }}
          if (statusCopy) {{
            if (status === 'failed') {{
              statusCopy.textContent = 'This run failed. Scroll down for the error details or return to the dashboard.';
            }} else if (status === 'completed' || status === 'published' || status === 'autoposted') {{
              statusCopy.textContent = 'This run is ready. Your tabs and generated assets are available below.';
            }} else if (status === 'autoposting') {{
              statusCopy.textContent = 'This run is posting to the configured channels. Refreshing the page will show the distribution results when they are written.';
            }} else {{
              statusCopy.textContent = 'This page updates while the run is in progress. When the build completes, the generated content and assets stay available here.';
            }}
          }}
          if ((status === 'completed' || status === 'published' || status === 'autoposted' || status === 'failed') && !window.__reloadedOnce) {{
            window.__reloadedOnce = true;
            window.location.reload();
          }}
        }}
        async function poll() {{
          try {{
            const response = await fetch('/api/run/' + encodeURIComponent(slug), {{ cache: 'no-store' }});
            if (!response.ok) return;
            const data = await response.json();
            applyStatus(data);
            if (!['completed', 'published', 'autoposted', 'failed'].includes(data.status)) {{
              window.setTimeout(poll, 3000);
            }}
          }} catch (_error) {{
            window.setTimeout(poll, 5000);
          }}
        }}
        if (!['completed', 'published', 'autoposted', 'failed'].includes({json.dumps(status)})) {{
          window.setTimeout(poll, 1500);
        }}
      }})();
    </script>
  </body>
</html>"""


def process_job(folder: Path, slug: str, repo_name: str, publish_now: bool) -> None:
    update_job_state(slug, status="running", error="", publish_error="", distribution_error="")
    try:
        job_dir = create_job_from_folder(folder)
        update_job_state(slug, job_dir=str(job_dir), preview_url=infer_output_url(slug))
        run_job(job_dir)
        update_job_state(slug, status="completed", has_output=True, repo_name=repo_name or slug, publish_error="")
        if publish_now:
            update_job_state(slug, status="publishing")
            visibility = load_env_config().get("DEFAULT_REPO_VISIBILITY", "public")
            try:
                result = subprocess.run(
                    [
                        "python3",
                        str(ROOT / "scripts" / "publish_job.py"),
                        str(job_dir),
                        "--repo",
                        repo_name or slug,
                        "--visibility",
                        visibility,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                site_url = result.stdout.strip().splitlines()[-1]
                update_job_state(slug, status="published", repo_name=repo_name or slug, site_url=site_url, publish_error="")
            except subprocess.CalledProcessError as exc:
                update_job_state(
                    slug,
                    status="completed",
                    has_output=True,
                    repo_name=repo_name or slug,
                    publish_error=format_subprocess_error(exc),
                )
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


def rerun_existing_job(slug: str) -> None:
    state = job_record(slug)
    update_job_state(slug, status="running", error="", publish_error="", distribution_error="", preview_url=infer_output_url(slug))
    try:
        job_dir = JOBS / slug
        if not (job_dir / "job.json").exists():
            raise FileNotFoundError(f"Missing job.json for {slug}")
        run_job(job_dir)
        update_job_state(
            slug,
            status="completed",
            has_output=True,
            repo_name=state.get("repo_name", "") or slug,
            site_url=state.get("site_url", ""),
        )
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


def autopost_existing_job(slug: str) -> None:
    update_job_state(slug, status="autoposting", error="", distribution_error="")
    try:
        job_dir = JOBS / slug
        result = subprocess.run(
            [preferred_distribution_python(), str(ROOT / "scripts" / "distribute_content.py"), str(job_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        output_text = result.stdout.strip().splitlines()
        distribution_summary = output_text[-1] if output_text else ""
        update_job_state(slug, status="autoposted", distribution_summary=distribution_summary)
    except subprocess.CalledProcessError as exc:
        update_job_state(slug, status="failed", error=format_subprocess_error(exc))
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


def publish_existing_job(slug: str, repo_name: str) -> None:
    update_job_state(slug, status="publishing", error="", publish_error="")
    try:
        job_dir = JOBS / slug
        visibility = load_env_config().get("DEFAULT_REPO_VISIBILITY", "public")
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "publish_job.py"),
                str(job_dir),
                "--repo",
                repo_name,
                "--visibility",
                visibility,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        site_url = result.stdout.strip().splitlines()[-1]
        update_job_state(slug, status="published", repo_name=repo_name, site_url=site_url, publish_error="")
    except subprocess.CalledProcessError as exc:
        fallback_status = "completed" if (JOBS / slug / "output" / "index.html").exists() else "failed"
        update_job_state(slug, status=fallback_status, publish_error=format_subprocess_error(exc))
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


def delete_job(slug: str) -> None:
    job_dir = JOBS / slug
    if job_dir.exists():
        shutil.rmtree(job_dir)
    remove_job_state(slug)
    with QUEUE_LOCK:
        queue = load_queue()
        queue.setdefault("jobs", {}).pop(slug, None)
        save_queue(queue)


def posting_worker_token() -> str:
    return load_env_config().get("POSTING_WORKER_TOKEN", "").strip()


def posting_bundle(slug: str) -> dict:
    job_dir = JOBS / slug
    manifest_path = job_dir / "job.json"
    if not manifest_path.exists():
        raise FileNotFoundError(slug)
    manifest = json.loads(manifest_path.read_text())
    output_dir = job_dir / "output"
    content_dir = output_dir / "content_pack"
    file_map: dict[str, str] = {}
    for _, _, filename in content_file_specs():
        file_path = content_dir / filename
        if file_path.exists():
            file_map[filename] = f"/preview/{slug}/content_pack/{filename}"
    video_path = output_dir / "edited_video" / "lead-magnet.mp4"
    transcript_path = output_dir / "transcripts" / "transcript.txt"
    return {
        "slug": slug,
        "manifest": manifest,
        "files": {
            "content_pack": file_map,
            "video": f"/preview/{slug}/edited_video/lead-magnet.mp4" if video_path.exists() else "",
            "transcript": f"/preview/{slug}/transcripts/transcript.txt" if transcript_path.exists() else "",
        },
        "posting_queue": queue_record(slug),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def parsed_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def worker_authorized(self) -> bool:
        expected = posting_worker_token()
        if not expected:
            return False
        header = self.headers.get("Authorization", "").strip()
        if header.startswith("Bearer "):
            return header.removeprefix("Bearer ").strip() == expected
        return self.headers.get("X-Posting-Worker-Token", "").strip() == expected

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, html: str, status: int = HTTPStatus.OK) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            message = query.get("message", [""])[0]
            self.send_html(dashboard_html(message))
            return
        if self.path.startswith("/run/"):
            slug = urllib.parse.unquote(self.path.removeprefix("/run/")).strip("/")
            if not slug:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_html(run_detail_html(slug))
            return
        if self.path.startswith("/api/run/"):
            slug = urllib.parse.unquote(self.path.removeprefix("/api/run/")).strip("/")
            if not slug:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_json(job_record(slug))
            return
        if self.path.startswith("/api/post-bundle/"):
            slug = urllib.parse.unquote(self.path.removeprefix("/api/post-bundle/")).strip("/")
            if not slug:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self.send_json(posting_bundle(slug))
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.path.startswith("/preview/"):
            raw = self.path.removeprefix("/preview/")
            parts = raw.split("/", 1)
            if not parts or not parts[0]:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            slug = parts[0]
            relative = parts[1] if len(parts) > 1 and parts[1] else "index.html"
            file_path = safe_relative_path(JOBS / slug / "output", relative)
            if not file_path or not file_path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if file_path.is_dir():
                self.send_html(directory_listing_html(slug, relative, file_path))
                return
            content = file_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(file_path))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/upload":
            self.handle_upload()
            return
        if self.path == "/publish":
            self.handle_publish()
            return
        if self.path == "/autopost":
            self.handle_autopost()
            return
        if self.path == "/queue-post":
            self.handle_queue_post()
            return
        if self.path == "/rerun":
            self.handle_rerun()
            return
        if self.path == "/delete":
            self.handle_delete()
            return
        if self.path == "/api/post-queue/claim":
            self.handle_worker_claim()
            return
        if self.path == "/api/post-queue/complete":
            self.handle_worker_complete()
            return
        if self.path == "/api/post-queue/fail":
            self.handle_worker_fail()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_upload(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        video_field = form["source_video"] if "source_video" in form else None
        if video_field is None or not getattr(video_field, "filename", ""):
            self.send_html(dashboard_html("Source video is required."), status=HTTPStatus.BAD_REQUEST)
            return

        title = form.getfirst("title", "").strip()
        folder_name = build_folder_name(title, video_field.filename)
        slug = slugify(folder_name)
        folder = INBOX / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        if not save_upload(video_field, folder / ("source" + Path(video_field.filename).suffix.lower())):
            self.send_html(dashboard_html("Failed to save source video."), status=HTTPStatus.BAD_REQUEST)
            return

        if "source_audio" in form and getattr(form["source_audio"], "filename", ""):
            audio_field = form["source_audio"]
            save_upload(audio_field, folder / ("source" + Path(audio_field.filename).suffix.lower()))
        if "source_vtt" in form and getattr(form["source_vtt"], "filename", ""):
            save_upload(form["source_vtt"], folder / "source.vtt")
        if "source_text" in form and getattr(form["source_text"], "filename", ""):
            save_upload(form["source_text"], folder / "source.txt")

        manifest = manifest_from_form(form)
        if manifest:
            (folder / "brief.json").write_text(json.dumps(manifest, indent=2))

        repo_name = form.getfirst("repo_name", "").strip()
        publish_now = form.getfirst("publish_now", "") == "1"
        update_job_state(
            slug,
            title=title or slug.replace("-", " ").title(),
            status="queued",
            created_at=now_iso(),
            repo_name=repo_name,
        )
        threading.Thread(target=process_job, args=(folder, slug, repo_name, publish_now), daemon=True).start()
        self.redirect(infer_run_url(slug))

    def handle_publish(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        slug = payload.get("slug", [""])[0].strip()
        repo_name = payload.get("repo_name", [""])[0].strip() or slug
        if not slug:
            self.send_html(dashboard_html("Missing job slug."), status=HTTPStatus.BAD_REQUEST)
            return
        threading.Thread(target=publish_existing_job, args=(slug, repo_name), daemon=True).start()
        message = urllib.parse.quote(f"Publishing {slug} to GitHub.")
        self.redirect(f"/?message={message}")

    def handle_autopost(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        slug = payload.get("slug", [""])[0].strip()
        if not slug:
            self.send_html(dashboard_html("Missing job slug."), status=HTTPStatus.BAD_REQUEST)
            return
        if not (JOBS / slug / "job.json").exists():
            self.send_html(dashboard_html(f"Could not find {slug}."), status=HTTPStatus.NOT_FOUND)
            return
        threading.Thread(target=autopost_existing_job, args=(slug,), daemon=True).start()
        self.redirect(infer_run_url(slug))

    def handle_queue_post(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        slug = payload.get("slug", [""])[0].strip()
        if not slug:
            self.send_html(dashboard_html("Missing job slug."), status=HTTPStatus.BAD_REQUEST)
            return
        if not (JOBS / slug / "job.json").exists():
            self.send_html(dashboard_html(f"Could not find {slug}."), status=HTTPStatus.NOT_FOUND)
            return
        queue_job_for_local_post(slug)
        self.redirect(infer_run_url(slug))

    def handle_rerun(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        slug = payload.get("slug", [""])[0].strip()
        if not slug:
            self.send_html(dashboard_html("Missing job slug."), status=HTTPStatus.BAD_REQUEST)
            return
        if not (JOBS / slug / "job.json").exists():
            self.send_html(dashboard_html(f"Could not find {slug}."), status=HTTPStatus.NOT_FOUND)
            return
        threading.Thread(target=rerun_existing_job, args=(slug,), daemon=True).start()
        self.redirect(infer_run_url(slug))

    def handle_delete(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        slug = payload.get("slug", [""])[0].strip()
        if not slug:
            self.send_html(dashboard_html("Missing job slug."), status=HTTPStatus.BAD_REQUEST)
            return
        delete_job(slug)
        message = urllib.parse.quote(f"Deleted {slug} from the dashboard.")
        self.redirect(f"/?message={message}#runs")

    def handle_worker_claim(self) -> None:
        if not self.worker_authorized():
            self.send_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return
        payload = self.parsed_json_body()
        worker_id = str(payload.get("worker_id", "")).strip() or "local-worker"
        item = claim_next_queue_item(worker_id)
        if not item:
            self.send_json({"job": None})
            return
        slug = item["slug"]
        self.send_json({"job": {**item, "bundle": posting_bundle(slug)}})

    def handle_worker_complete(self) -> None:
        if not self.worker_authorized():
            self.send_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return
        payload = self.parsed_json_body()
        slug = str(payload.get("slug", "")).strip()
        worker_id = str(payload.get("worker_id", "")).strip() or "local-worker"
        results = payload.get("results", {})
        if not slug or not isinstance(results, dict):
            self.send_json({"error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
            return
        complete_queue_item(slug, worker_id, results)
        self.send_json({"ok": True})

    def handle_worker_fail(self) -> None:
        if not self.worker_authorized():
            self.send_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return
        payload = self.parsed_json_body()
        slug = str(payload.get("slug", "")).strip()
        worker_id = str(payload.get("worker_id", "")).strip() or "local-worker"
        error = str(payload.get("error", "")).strip() or "Unknown worker error."
        if not slug:
            self.send_json({"error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
            return
        fail_queue_item(slug, worker_id, error)
        self.send_json({"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    default_host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8090"))
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    ensure_runtime_dirs()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
