#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "inbox"
JOBS = ROOT / "jobs"

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav"}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_manifest(slug: str) -> dict:
    title = slug.replace("-", " ").title()
    return {
        "slug": slug,
        "title": title,
        "headline": f"Watch {title} come together from one real source recording.",
        "subheadline": "This page was generated from the Claude Content Factory pipeline.",
        "lead": "Review the video, the checklist, and the companion PDF, then send people to the full certification if they want the deeper path.",
        "cta_url": "https://jbellsolutions.github.io/claude-code-ecosystem-certification/",
        "cta_label": "Take the full level one certification here for free",
        "pdf_title": f"{title} Companion Guide",
        "kit_form_action": "",
        "kit_button_text": "Get Access",
        "checklist": [
            "Main lesson one",
            "Main lesson two",
            "Main lesson three"
        ],
        "manual_segments": []
    }


def locate_inputs(folder: Path) -> tuple[Path | None, Path | None, Path | None]:
    video = audio = vtt = None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in VIDEO_EXTS and video is None:
            video = path
        elif path.suffix.lower() in AUDIO_EXTS and audio is None:
            audio = path
        elif path.suffix.lower() == ".vtt" and vtt is None:
            vtt = path
    return video, audio, vtt


def create_job_from_folder(folder: Path) -> Path:
    slug = slugify(folder.name)
    job_dir = JOBS / slug
    if job_dir.exists():
        return job_dir

    brief_path = folder / "brief.json"
    manifest = default_manifest(slug)
    if brief_path.exists():
        manifest.update(json.loads(brief_path.read_text()))

    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "output").mkdir(parents=True, exist_ok=True)

    video, audio, vtt = locate_inputs(folder)
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

    (job_dir / "job.json").write_text(json.dumps(manifest, indent=2))
    return job_dir


def main() -> None:
    print(f"Watching {INBOX}")
    while True:
        for folder in sorted(INBOX.iterdir()):
            if not folder.is_dir():
                continue
            if (folder / ".processed").exists() or (folder / ".processing").exists():
                continue
            video, _, _ = locate_inputs(folder)
            if not video:
                continue
            (folder / ".processing").write_text("")
            job_dir = create_job_from_folder(folder)
            subprocess.run(["python3", str(ROOT / "scripts" / "run_job.py"), str(job_dir)], check=True)
            (folder / ".processing").unlink(missing_ok=True)
            (folder / ".processed").write_text(str(job_dir))
            print(f"Processed {folder.name} -> {job_dir}")
        time.sleep(5)


if __name__ == "__main__":
    main()
