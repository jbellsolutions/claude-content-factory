#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from distribute_content import build_results
from factory_ingest import load_env_config
from runtime_paths import DATA_ROOT


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def post_json(url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=auth_headers(token),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(base_url: str, relative_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    absolute_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", relative_url.lstrip("/"))
    with urllib.request.urlopen(absolute_url, timeout=600) as response:
        destination.write_bytes(response.read())


def materialize_job(base_url: str, bundle: dict) -> Path:
    slug = bundle["slug"]
    worker_root = DATA_ROOT / "posting-worker" / slug
    if worker_root.exists():
        shutil.rmtree(worker_root)
    (worker_root / "output" / "content_pack").mkdir(parents=True, exist_ok=True)
    (worker_root / "output" / "edited_video").mkdir(parents=True, exist_ok=True)
    (worker_root / "output" / "transcripts").mkdir(parents=True, exist_ok=True)
    (worker_root / "job.json").write_text(json.dumps(bundle["manifest"], indent=2))

    for filename, relative_url in bundle.get("files", {}).get("content_pack", {}).items():
        download_file(base_url, relative_url, worker_root / "output" / "content_pack" / filename)

    video_url = bundle.get("files", {}).get("video", "")
    if video_url:
        download_file(base_url, video_url, worker_root / "output" / "edited_video" / "lead-magnet.mp4")

    transcript_url = bundle.get("files", {}).get("transcript", "")
    if transcript_url:
        download_file(base_url, transcript_url, worker_root / "output" / "transcripts" / "transcript.txt")

    return worker_root


def process_once(base_url: str, token: str, worker_id: str) -> bool:
    claim_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "api/post-queue/claim")
    response = post_json(claim_url, token, {"worker_id": worker_id})
    job = response.get("job")
    if not job:
        return False

    slug = job["slug"]
    try:
        job_dir = materialize_job(base_url, job["bundle"])
        results = build_results(job_dir, job.get("requested_channels", []))
        complete_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "api/post-queue/complete")
        post_json(complete_url, token, {"slug": slug, "worker_id": worker_id, "results": results})
    except Exception as exc:
        fail_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "api/post-queue/fail")
        post_json(fail_url, token, {"slug": slug, "worker_id": worker_id, "error": str(exc)})
    return True


def main() -> None:
    env = load_env_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=env.get("POSTING_BRIDGE_URL", "").strip())
    parser.add_argument("--token", default=env.get("POSTING_WORKER_TOKEN", "").strip())
    parser.add_argument("--worker-id", default=env.get("POSTING_WORKER_ID", "").strip() or socket.gethostname())
    parser.add_argument("--poll-seconds", type=int, default=int(env.get("POSTING_WORKER_POLL_SECONDS", "20")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not args.server:
        raise SystemExit("Set POSTING_BRIDGE_URL or pass --server.")
    if not args.token:
        raise SystemExit("Set POSTING_WORKER_TOKEN or pass --token.")

    if args.once:
        process_once(args.server, args.token, args.worker_id)
        return

    while True:
        found = process_once(args.server, args.token, args.worker_id)
        if not found:
            time.sleep(max(args.poll_seconds, 5))


if __name__ == "__main__":
    main()
