#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def run(cmd: list[str], cwd: Path, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        capture_output=capture,
        text=True,
    )


def resolve_owner(output_dir: Path) -> str:
    if os.environ.get("GITHUB_OWNER", "").strip():
        return os.environ["GITHUB_OWNER"].strip()
    result = run(["gh", "api", "user", "-q", ".login"], output_dir, capture=True)
    return result.stdout.strip()


def repo_exists(repo_ref: str, output_dir: Path) -> bool:
    result = run(["gh", "repo", "view", repo_ref], output_dir, check=False, capture=True)
    return result.returncode == 0


def ensure_remote(output_dir: Path, repo_ref: str) -> None:
    remote_url = f"https://github.com/{repo_ref}.git"
    current = run(["git", "remote"], output_dir, capture=True).stdout.split()
    if "origin" not in current:
        run(["git", "remote", "add", "origin", remote_url], output_dir)
        return
    run(["git", "remote", "set-url", "origin", remote_url], output_dir)


def enable_pages(repo_ref: str, output_dir: Path) -> None:
    existing = run(["gh", "api", f"repos/{repo_ref}/pages"], output_dir, check=False, capture=True)
    if existing.returncode == 0:
        return
    run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo_ref}/pages",
            "-f",
            "source[branch]=main",
            "-f",
            "source[path]=/",
        ],
        output_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--visibility", default="public", choices=["public", "private"])
    args = parser.parse_args()

    job_dir = Path(args.job_dir).resolve()
    manifest = json.loads((job_dir / "job.json").read_text())
    output_dir = job_dir / "output"

    if not output_dir.exists():
        raise SystemExit("Run the job before publishing.")

    run(["git", "init"], output_dir)
    run(["git", "checkout", "-B", "main"], output_dir)
    run(["git", "config", "user.name", "Codex"], output_dir)
    run(["git", "config", "user.email", "codex@local"], output_dir)
    run(["git", "add", "."], output_dir)
    run(["git", "commit", "--allow-empty", "-m", f"Publish {manifest['title']}"], output_dir)

    owner = resolve_owner(output_dir)
    repo_ref = f"{owner}/{args.repo}"
    if not repo_exists(repo_ref, output_dir):
        run(["gh", "repo", "create", repo_ref, f"--{args.visibility}"], output_dir)

    ensure_remote(output_dir, repo_ref)
    run(["git", "push", "--force-with-lease", "-u", "origin", "main"], output_dir)
    enable_pages(repo_ref, output_dir)
    print(f"https://{owner}.github.io/{args.repo}/")


if __name__ == "__main__":
    main()
