#!/usr/bin/env python3
from __future__ import annotations

import time

from factory_ingest import INBOX, create_job_from_folder, locate_inputs, run_job


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
            run_job(job_dir)
            (folder / ".processing").unlink(missing_ok=True)
            (folder / ".processed").write_text(str(job_dir))
            print(f"Processed {folder.name} -> {job_dir}")
        time.sleep(5)


if __name__ == "__main__":
    main()
