#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from factory_ingest import DEFAULT_CTA, load_env_config, slugify
from runtime_paths import JOBS, ensure_runtime_dirs


def infer_title(slug: str) -> str:
    return slug.replace("-", " ").title()


def copy_input(src: str | None, dest_dir: Path, name: str) -> str | None:
    if not src:
        return None
    source = Path(src).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    destination = dest_dir / name
    shutil.copy2(source, destination)
    return f"input/{name}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--title")
    parser.add_argument("--source-video", required=True)
    parser.add_argument("--source-audio")
    parser.add_argument("--source-vtt")
    parser.add_argument("--source-text")
    parser.add_argument("--headline")
    parser.add_argument("--subheadline")
    parser.add_argument("--lead")
    args = parser.parse_args()

    ensure_runtime_dirs()
    env = load_env_config()
    slug = slugify(args.slug)
    job_dir = JOBS / slug
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    title = args.title or infer_title(slug)
    manifest = {
        "slug": slug,
        "title": title,
        "headline": args.headline or f"Watch {title} come together from one real source recording.",
        "subheadline": args.subheadline
        or "This page was generated from a reusable factory pipeline that cleans transcripts, renders a branded video, builds a PDF companion, and prepares a publishable landing page.",
        "lead": args.lead
        or "Use this as a starting point, then tighten the copy, CTA, and checklist before publishing if you want a sharper offer.",
        "cta_url": env.get("DEFAULT_CTA_URL", DEFAULT_CTA),
        "cta_label": "Take the full level one certification here for free",
        "pdf_title": f"{title} Companion Guide",
        "kit_form_action": env.get("KIT_FORM_ACTION", ""),
        "kit_button_text": env.get("KIT_BUTTON_TEXT", "Get Access"),
        "kit_tag": env.get("KIT_TAG", ""),
        "generate_content_pack": True,
        "brand_name": env.get("BRAND_NAME", title),
        "target_audience": env.get("TARGET_AUDIENCE", "Founders, executives, operators, and employees leveling up with AI"),
        "voice_notes": env.get("VOICE_NOTES", "Authority content. Useful, specific, not salesy, not promotional."),
        "checklist": [
            "Key lesson one",
            "Key lesson two",
            "Key lesson three"
        ],
        "manual_segments": [],
        "source_video": copy_input(args.source_video, input_dir, "source" + Path(args.source_video).suffix),
        "source_audio": copy_input(args.source_audio, input_dir, "source" + Path(args.source_audio).suffix)
        if args.source_audio
        else None,
        "source_vtt": copy_input(args.source_vtt, input_dir, "source.vtt") if args.source_vtt else None,
        "source_text": copy_input(args.source_text, input_dir, "source.txt") if args.source_text else None
    }

    (job_dir / "job.json").write_text(json.dumps(manifest, indent=2))
    print(job_dir)


if __name__ == "__main__":
    main()
