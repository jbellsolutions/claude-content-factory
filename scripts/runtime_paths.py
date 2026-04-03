#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("CONTENT_FACTORY_DATA_ROOT", str(CODE_ROOT))).expanduser().resolve()

INBOX = DATA_ROOT / "inbox"
JOBS = DATA_ROOT / "jobs"
PUBLISHED = DATA_ROOT / "published"


def ensure_runtime_dirs() -> None:
    for path in [INBOX, JOBS, PUBLISHED]:
        path.mkdir(parents=True, exist_ok=True)


def env_file_candidates() -> list[Path]:
    return [
        DATA_ROOT / ".env",
        DATA_ROOT / "config" / ".env",
        CODE_ROOT / ".env",
        CODE_ROOT / "config" / ".env",
    ]
