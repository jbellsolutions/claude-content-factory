#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


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
    run(["git", "config", "user.name", "Codex"], output_dir)
    run(["git", "config", "user.email", "codex@local"], output_dir)
    run(["git", "add", "."], output_dir)
    run(["git", "commit", "-m", f"Publish {manifest['title']}"], output_dir)
    run(["gh", "repo", "create", args.repo, f"--{args.visibility}", "--source=.", "--push"], output_dir)
    owner = subprocess.check_output(["gh", "repo", "view", args.repo, "--json", "owner", "-q", ".owner.login"], cwd=str(output_dir), text=True).strip()
    run(
        [
            "gh", "api", "--method", "POST",
            f"repos/{owner}/{args.repo}/pages",
            "-f", "source[branch]=main",
            "-f", "source[path]=/"
        ],
        output_dir,
    )
    print(f"https://{owner}.github.io/{args.repo}/")


if __name__ == "__main__":
    main()
