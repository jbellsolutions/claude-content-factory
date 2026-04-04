#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from runtime_paths import CODE_ROOT, INBOX, JOBS, ensure_runtime_dirs, env_file_candidates

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav"}
TEXT_EXTS = {".txt"}
DEFAULT_CTA = "https://jbellsolutions.github.io/claude-code-ecosystem-certification/"


def load_env_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for path in env_file_candidates():
        if not path.exists():
            continue
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        config[key] = value
    return config


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_manifest(slug: str, env: dict[str, str] | None = None) -> dict:
    env = env or {}
    title = slug.replace("-", " ").title()
    return {
        "slug": slug,
        "title": title,
        "headline": f"Watch {title} come together from one real source recording.",
        "subheadline": "This page was generated from the Claude Content Factory pipeline.",
        "lead": "Review the video, the checklist, and the companion PDF, then send people to the full certification if they want the deeper path.",
        "cta_url": env.get("DEFAULT_CTA_URL", DEFAULT_CTA),
        "cta_label": "Take the full level one certification here for free",
        "pdf_title": f"{title} Companion Guide",
        "kit_form_action": env.get("KIT_FORM_ACTION", ""),
        "kit_button_text": env.get("KIT_BUTTON_TEXT", "Get Access"),
        "kit_tag": env.get("KIT_TAG", ""),
        "generate_content_pack": True,
        "brand_name": env.get("BRAND_NAME", title),
        "target_audience": env.get("TARGET_AUDIENCE", "Founders, executives, operators, and employees leveling up with AI"),
        "voice_notes": env.get("VOICE_NOTES", "Direct, tactical, founder-led, authority-building, clear, high-agency, and useful. Sound like a real operator. Not salesy, not promotional, not generic. Use light platform-native emojis where natural."),
        "checklist": [
            "Main lesson one",
            "Main lesson two",
            "Main lesson three"
        ],
        "manual_segments": []
    }


def locate_inputs(folder: Path) -> tuple[Path | None, Path | None, Path | None, Path | None]:
    video = audio = vtt = text = None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in VIDEO_EXTS and video is None:
            video = path
        elif path.suffix.lower() in AUDIO_EXTS and audio is None:
            audio = path
        elif path.suffix.lower() == ".vtt" and vtt is None:
            vtt = path
        elif path.suffix.lower() in TEXT_EXTS and text is None:
            text = path
    return video, audio, vtt, text


def create_job_from_folder(folder: Path) -> Path:
    ensure_runtime_dirs()
    env = load_env_config()
    slug = slugify(folder.name)
    job_dir = JOBS / slug
    if job_dir.exists():
        return job_dir

    brief_path = folder / "brief.json"
    manifest = default_manifest(slug, env)
    if brief_path.exists():
        manifest.update(json.loads(brief_path.read_text()))

    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "output").mkdir(parents=True, exist_ok=True)

    video, audio, vtt, text = locate_inputs(folder)
    if video:
        dest = input_dir / ("source" + video.suffix.lower())
        shutil.copy2(video, dest)
        manifest["source_video"] = str(dest.relative_to(job_dir))
    if audio:
        dest = input_dir / ("source" + audio.suffix.lower())
        shutil.copy2(audio, dest)
        manifest["source_audio"] = str(dest.relative_to(job_dir))
    if vtt:
        dest = input_dir / "source.vtt"
        shutil.copy2(vtt, dest)
        manifest["source_vtt"] = str(dest.relative_to(job_dir))
    if text:
        dest = input_dir / "source.txt"
        shutil.copy2(text, dest)
        manifest["source_text"] = str(dest.relative_to(job_dir))

    (job_dir / "job.json").write_text(json.dumps(manifest, indent=2))
    return job_dir


def run_job(job_dir: Path) -> None:
    result = subprocess.run(
        ["python3", str(CODE_ROOT / "scripts" / "run_job.py"), str(job_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    log_text = "\n".join(
        part for part in [
            "=== STDOUT ===",
            result.stdout.strip(),
            "",
            "=== STDERR ===",
            result.stderr.strip(),
        ] if part is not None
    ).strip() + "\n"
    (job_dir / "run.log").write_text(log_text)
    if result.returncode != 0:
        error_text = result.stderr.strip() or result.stdout.strip() or f"run_job.py exited with status {result.returncode}"
        raise RuntimeError(error_text)
