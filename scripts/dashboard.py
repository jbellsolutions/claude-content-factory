#!/usr/bin/env python3
from __future__ import annotations

import cgi
import json
import mimetypes
import subprocess
import threading
import traceback
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from factory_ingest import INBOX, JOBS, create_job_from_folder, load_env_config, run_job, slugify


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".dashboard_state.json"
STATE_LOCK = threading.Lock()


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


def update_job_state(slug: str, **changes: object) -> None:
    with STATE_LOCK:
        state = load_state()
        jobs = state.setdefault("jobs", {})
        current = jobs.get(slug, {})
        current.update(changes)
        current["updated_at"] = now_iso()
        jobs[slug] = current
        save_state(state)


def all_jobs() -> list[dict]:
    state = load_state()
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
            }
        )
    items.sort(key=lambda item: (item.get("updated_at", ""), item["slug"]), reverse=True)
    return items


def infer_output_url(slug: str) -> str:
    return f"/preview/{slug}/index.html"


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
        preview = infer_output_url(slug) if job.get("has_output") else ""
        preview_link = f'<a class="ghost" href="{preview}">Preview</a>' if preview else ""
        live_link = f'<a class="ghost" href="{site_url}" target="_blank" rel="noreferrer">Live Site</a>' if site_url else ""
        publish_form = ""
        if job.get("has_output") and status not in {"publishing", "published"}:
            publish_form = f"""
            <form class="inline-form" method="post" action="/publish">
              <input type="hidden" name="slug" value="{slug}" />
              <input type="text" name="repo_name" value="{repo_name or slug}" />
              <button type="submit">Publish</button>
            </form>
            """
        error_block = f'<p class="error">{error}</p>' if error else ""
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
                {preview_link}
                {live_link}
                {publish_form}
              </div>
              {error_block}
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
      .topbar,.panel,.jobs{{margin-top:24px;border-radius:32px;background:var(--surface);border:1px solid rgba(255,255,255,.34);box-shadow:var(--shadow);backdrop-filter:blur(18px)}}
      .topbar{{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:20px 24px}}
      .eyebrow{{margin:0 0 8px;text-transform:uppercase;letter-spacing:.08em;font-size:.76rem;font-weight:800;color:var(--accent)}}
      h1,h2,h3{{margin:0;font-family:"New York","Iowan Old Style",Georgia,serif}} h1{{font-size:clamp(2.6rem,5vw,4.4rem);line-height:.95}} h2{{font-size:clamp(2rem,4vw,3rem);line-height:.98}} h3{{font-size:1.5rem;line-height:1.05}}
      p{{margin:0}} .muted{{color:var(--muted);line-height:1.65;max-width:56rem}}
      .flash{{margin-top:18px;padding:14px 18px;border-radius:18px;background:rgba(47,116,86,.12);border:1px solid rgba(47,116,86,.18);color:var(--green)}}
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
      .job-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px;margin-top:22px}}
      .job-card{{padding:20px;border-radius:24px;background:var(--surface-2);border:1px solid rgba(255,255,255,.34)}}
      .job-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}
      .status{{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:0 12px;border-radius:999px;font-size:.85rem;font-weight:800;text-transform:capitalize}}
      .status-queued,.status-publishing{{background:rgba(155,92,24,.12);color:var(--amber)}}
      .status-running{{background:rgba(21,53,64,.12);color:var(--ink)}}
      .status-completed,.status-published{{background:rgba(47,116,86,.12);color:var(--green)}}
      .status-failed{{background:rgba(141,47,47,.12);color:var(--red)}}
      .meta{{margin-top:10px;color:var(--muted)}}
      .job-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}}
      .inline-form{{display:flex;gap:10px;flex-wrap:wrap}} .inline-form input{{width:220px;padding:12px 14px;border-radius:999px}}
      .empty{{margin-top:18px;color:var(--muted)}}
      .error{{margin-top:14px;padding:12px 14px;border-radius:16px;background:rgba(141,47,47,.08);color:var(--red);white-space:pre-wrap}}
      .tips{{padding:22px;border-radius:26px;background:linear-gradient(180deg,rgba(8,23,33,.98),rgba(14,36,48,.92));color:#f8efe1}}
      .tips p,.tips li{{color:rgba(248,239,225,.84);line-height:1.6}} .tips ul{{margin:16px 0 0;padding-left:20px}}
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
      <section class="panel">
        <div>
          <p class="eyebrow">New Job</p>
          <h2>Drop in a video and run the pipeline.</h2>
          <form method="post" action="/upload" enctype="multipart/form-data">
            <div class="form-grid">
              <div class="field"><label for="title">Title</label><input id="title" type="text" name="title" placeholder="How To Use Claude In 15 Minutes" required /></div>
              <div class="field"><label for="repo_name">Repo Name</label><input id="repo_name" type="text" name="repo_name" placeholder="claude-in-15-minutes-lead-magnet" /></div>
              <div class="field-full"><label for="headline">Headline</label><input id="headline" type="text" name="headline" placeholder="Build live inside Claude in one short walkthrough" /></div>
              <div class="field-full"><label for="subheadline">Subheadline</label><input id="subheadline" type="text" name="subheadline" placeholder="Cover Claude Chat, Claude Cowork, and Claude Code in one pass." /></div>
              <div class="field-full"><label for="lead">Lead</label><textarea id="lead" name="lead" placeholder="Short lead paragraph for the page."></textarea></div>
              <div class="field-full"><label for="checklist">Checklist</label><textarea id="checklist" name="checklist" placeholder="One item per line"></textarea></div>
              <div class="field"><label for="cta_url">CTA URL</label><input id="cta_url" type="url" name="cta_url" value="{default_cta}" /></div>
              <div class="field"><label for="cta_label">CTA Label</label><input id="cta_label" type="text" name="cta_label" value="Take the full level one certification here for free" /></div>
              <div class="field-full"><label for="video">Source Video</label><input id="video" type="file" name="source_video" accept=".mp4,.mov,.m4v" required /></div>
              <div class="field"><label for="audio">Optional Audio</label><input id="audio" type="file" name="source_audio" accept=".m4a,.mp3,.wav" /></div>
              <div class="field"><label for="vtt">Optional VTT</label><input id="vtt" type="file" name="source_vtt" accept=".vtt" /></div>
            </div>
            <label class="checkline"><input type="checkbox" name="publish_now" value="1" /> Publish to GitHub after build</label>
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
      <section class="jobs">
        <p class="eyebrow">Recent Jobs</p>
        <h2>Track builds, previews, and publishing.</h2>
        <div class="job-grid">
          {jobs_markup}
        </div>
      </section>
    </div>
  </body>
</html>"""


def manifest_from_form(form: cgi.FieldStorage) -> dict:
    checklist = [line.strip() for line in form.getfirst("checklist", "").splitlines() if line.strip()]
    manifest = {
        "title": form.getfirst("title", "").strip(),
        "headline": form.getfirst("headline", "").strip(),
        "subheadline": form.getfirst("subheadline", "").strip(),
        "lead": form.getfirst("lead", "").strip(),
        "cta_url": form.getfirst("cta_url", "").strip(),
        "cta_label": form.getfirst("cta_label", "").strip(),
        "checklist": checklist,
    }
    return {key: value for key, value in manifest.items() if value}


def process_job(folder: Path, slug: str, repo_name: str, publish_now: bool) -> None:
    update_job_state(slug, status="running", error="")
    try:
        job_dir = create_job_from_folder(folder)
        update_job_state(slug, job_dir=str(job_dir), preview_url=infer_output_url(slug))
        run_job(job_dir)
        update_job_state(slug, status="completed", has_output=True)
        if publish_now:
            update_job_state(slug, status="publishing")
            visibility = load_env_config().get("DEFAULT_REPO_VISIBILITY", "public")
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
            update_job_state(slug, status="published", repo_name=repo_name or slug, site_url=site_url)
        else:
            update_job_state(slug, repo_name=repo_name or slug)
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


def publish_existing_job(slug: str, repo_name: str) -> None:
    update_job_state(slug, status="publishing", error="")
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
        update_job_state(slug, status="published", repo_name=repo_name, site_url=site_url)
    except Exception:
        update_job_state(slug, status="failed", error=traceback.format_exc(limit=12))


class DashboardHandler(BaseHTTPRequestHandler):
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
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            message = query.get("message", [""])[0]
            self.send_html(dashboard_html(message))
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
            if not file_path or not file_path.exists() or file_path.is_dir():
                self.send_error(HTTPStatus.NOT_FOUND)
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
        message = urllib.parse.quote(f"Queued {title or slug} for processing.")
        self.redirect(f"/?message={message}")

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

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
