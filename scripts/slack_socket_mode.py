#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from factory_ingest import INBOX, create_job_from_folder, load_env_config, run_job, slugify
from runtime_paths import CODE_ROOT, DATA_ROOT, ensure_runtime_dirs

STATE_FILE = DATA_ROOT / ".slack_state.json"
SUPPORTED_EXTS = {".mp4", ".mov", ".m4v", ".m4a", ".mp3", ".wav", ".vtt", ".txt"}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_keys": []}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def parse_allowed_channels(config: dict[str, str]) -> set[str]:
    raw = config.get("SLACK_ALLOWED_CHANNELS", "").strip()
    if not raw:
        return set()
    return {value.strip() for value in raw.split(",") if value.strip()}


def extract_json_block(text: str) -> dict:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def parse_brief_text(text: str) -> dict:
    brief = extract_json_block(text)
    if brief:
        return brief

    data: dict[str, object] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            current_list_key = None
            continue
        if current_list_key and line.startswith("- "):
            data.setdefault(current_list_key, [])
            assert isinstance(data[current_list_key], list)
            data[current_list_key].append(line[2:].strip())
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_ ]*):\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip()
        if key == "checklist":
            current_list_key = "checklist"
            if value:
                data["checklist"] = [item.strip() for item in value.split("|") if item.strip()]
            else:
                data["checklist"] = []
            continue
        data[key] = value
        current_list_key = None
    return data


def download_file(token: str, url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request) as response:
        dest.write_bytes(response.read())


def pick_filename(file_info: dict) -> str | None:
    name = file_info.get("name") or file_info.get("title") or ""
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return None
    if suffix == ".vtt":
        return "source.vtt"
    if suffix == ".txt":
        return "source.txt"
    if suffix in {".mp4", ".mov", ".m4v"}:
        return "source" + suffix
    return "source" + suffix


def folder_name(event: dict) -> str:
    title = event.get("text") or ""
    if title:
        title = title.splitlines()[0].strip()[:80]
    stem = slugify(title) or "slack-upload"
    timestamp = str(event.get("ts", "upload")).replace(".", "-")
    return f"{stem}-{timestamp}"


def repo_name_for(folder: Path, brief: dict, config: dict[str, str]) -> str:
    if isinstance(brief.get("repo_name"), str) and brief["repo_name"].strip():
        return brief["repo_name"].strip()
    prefix = config.get("SLACK_REPO_PREFIX", "").strip()
    return f"{prefix}{folder.name}" if prefix else folder.name


def maybe_publish(job_dir: Path, brief: dict, config: dict[str, str]) -> None:
    auto_publish = config.get("SLACK_AUTO_PUBLISH", "").lower() in {"1", "true", "yes"}
    if not auto_publish:
        return
    visibility = config.get("DEFAULT_REPO_VISIBILITY", "public")
    repo_name = repo_name_for(job_dir, brief, config)
    subprocess_cmd = [
        "python3",
        str(CODE_ROOT / "scripts" / "publish_job.py"),
        str(job_dir),
        "--repo",
        repo_name,
        "--visibility",
        visibility,
    ]
    subprocess.run(subprocess_cmd, check=True)


def preferred_distribution_python(config: dict[str, str]) -> str:
    configured = config.get("BROWSER_USE_PYTHON", "").strip()
    if configured and Path(configured).exists():
        return configured
    candidates = [
        Path.home() / ".browser-use-env" / "bin" / "python",
        CODE_ROOT / ".browser-use-env" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def maybe_distribute(job_dir: Path, config: dict[str, str]) -> None:
    auto_distribute = config.get("SLACK_AUTO_APPROVE_POST", "").lower() in {"1", "true", "yes"}
    if not auto_distribute:
        return
    subprocess.run(
        [preferred_distribution_python(config), str(CODE_ROOT / "scripts" / "distribute_content.py"), str(job_dir)],
        check=True,
    )


def handle_message(client: WebClient, event: dict, config: dict[str, str], state: dict) -> None:
    files = event.get("files") or []
    if not files:
        return
    allowed_channels = parse_allowed_channels(config)
    channel = event.get("channel", "")
    if allowed_channels and channel not in allowed_channels:
        return

    dedupe_key = event.get("client_msg_id") or f"{channel}:{event.get('ts', '')}"
    processed_keys = set(state.get("processed_keys", []))
    if dedupe_key in processed_keys:
        return

    folder = INBOX / folder_name(event)
    folder.mkdir(parents=True, exist_ok=True)

    brief = parse_brief_text(event.get("text", ""))
    if brief:
        (folder / "brief.json").write_text(json.dumps(brief, indent=2))

    token = config["SLACK_BOT_TOKEN"]
    downloaded = 0
    for file_stub in files:
        info = client.files_info(file=file_stub["id"]).get("file", file_stub)
        filename = pick_filename(info)
        if not filename:
            continue
        url = info.get("url_private_download") or info.get("url_private")
        if not url:
            continue
        download_file(token, url, folder / filename)
        downloaded += 1

    if downloaded == 0:
        return

    job_dir = create_job_from_folder(folder)
    run_job(job_dir)
    maybe_publish(job_dir, brief, config)
    maybe_distribute(job_dir, config)
    (folder / ".processed").write_text(str(job_dir))

    processed_keys.add(dedupe_key)
    state["processed_keys"] = sorted(processed_keys)[-500:]
    save_state(state)
    print(f"Processed Slack upload -> {job_dir}")


def process_socket_request(socket_client: SocketModeClient, request: SocketModeRequest) -> None:
    if request.type != "events_api":
        return
    socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
    payload = request.payload or {}
    event = payload.get("event", {})
    if event.get("type") != "message" or event.get("subtype") in {"message_changed", "message_deleted"}:
        return
    config = load_env_config()
    required = {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SystemExit(f"Missing Slack config: {', '.join(missing)}")
    handle_message(socket_client.web_client, event, config, load_state())


def main() -> None:
    ensure_runtime_dirs()
    config = load_env_config()
    required = {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SystemExit(f"Missing Slack config: {', '.join(missing)}")

    web_client = WebClient(token=config["SLACK_BOT_TOKEN"])
    socket_client = SocketModeClient(app_token=config["SLACK_APP_TOKEN"], web_client=web_client)
    socket_client.socket_mode_request_listeners.append(process_socket_request)
    socket_client.connect()
    print("Slack Socket Mode listener is running.")
    while True:
        time.sleep(5)


if __name__ == "__main__":
    main()
