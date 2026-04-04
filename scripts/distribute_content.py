#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory_ingest import load_env_config


CHANNEL_FILES = {
    "facebook_post": "facebook-post.md",
    "linkedin_post": "linkedin-post.md",
    "linkedin_article": "linkedin-article.md",
    "medium_article": "medium-article.md",
    "substack_post": "substack-post.md",
    "newsletter": "newsletter.md",
    "youtube_package": "youtube-package.md",
}

DEFAULT_BROWSER_USE_CHANNELS = [
    "facebook_post",
    "linkedin_post",
    "linkedin_article",
    "medium_article",
    "substack_post",
    "youtube_package",
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def read_text(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2))


def truthy(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def extract_section(text: str, label: str) -> str:
    pattern = rf"^{re.escape(label)}:\s*(.*?)(?=^[A-Z][A-Za-z0-9 /&-]*:\s|\Z)"
    match = re.search(pattern, text, flags=re.M | re.S)
    return match.group(1).strip() if match else ""


def plain_text_to_html(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    html_blocks = []
    for block in blocks:
        html_blocks.append(f"<p>{html.escape(block).replace(chr(10), '<br />')}</p>")
    return "\n".join(html_blocks)


def http_json(method: str, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def create_kit_broadcast(job_dir: Path, manifest: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    api_key = env.get("KIT_API_KEY", "").strip()
    if not api_key:
        return {"status": "skipped", "reason": "KIT_API_KEY is not configured."}

    newsletter_text = read_text(job_dir / "output" / "content_pack" / CHANNEL_FILES["newsletter"])
    if not newsletter_text:
        return {"status": "skipped", "reason": "newsletter.md was not generated."}

    subject = extract_section(newsletter_text, "Recommended Subject") or manifest["title"]
    preview_text = extract_section(newsletter_text, "Preview Text") or manifest.get("headline", "")
    body = extract_section(newsletter_text, "Newsletter Body") or newsletter_text
    soft_cta = extract_section(newsletter_text, "Soft CTA")
    if soft_cta:
        body = body.rstrip() + "\n\n" + soft_cta

    subscriber_filter: list[dict[str, Any]]
    segment_ids = [segment.strip() for segment in env.get("KIT_SEGMENT_IDS", "").split(",") if segment.strip()]
    tag_ids = [tag.strip() for tag in env.get("KIT_TAG_IDS", "").split(",") if tag.strip()]
    if segment_ids:
        subscriber_filter = [{"all": [{"type": "segment", "ids": [int(item) for item in segment_ids]}]}]
    elif tag_ids:
        subscriber_filter = [{"all": [{"type": "tag", "ids": [int(item) for item in tag_ids]}]}]
    else:
        subscriber_filter = [{"all": [{"type": "all_subscribers"}]}]

    payload: dict[str, Any] = {
        "content": plain_text_to_html(body),
        "description": manifest["title"],
        "public": truthy(env.get("KIT_PUBLIC_POST"), default=True),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "preview_text": preview_text,
        "subject": subject,
        "subscriber_filter": subscriber_filter,
        "send_at": None,
    }
    if env.get("KIT_EMAIL_TEMPLATE_ID", "").strip():
        payload["email_template_id"] = int(env["KIT_EMAIL_TEMPLATE_ID"])
    if env.get("KIT_EMAIL_ADDRESS", "").strip():
        payload["email_address"] = env["KIT_EMAIL_ADDRESS"].strip()

    response = http_json(
        "POST",
        "https://api.kit.com/v4/broadcasts",
        {"X-Kit-Api-Key": api_key},
        payload,
    )
    return {
        "status": "posted",
        "provider": "kit",
        "broadcast_id": response.get("broadcast", {}).get("id"),
        "public_url": response.get("broadcast", {}).get("public_url", ""),
    }


def browser_use_enabled(env: dict[str, str]) -> bool:
    return truthy(env.get("BROWSER_USE_ENABLED"), default=False)


def browser_task_for(channel_key: str, text: str, job_dir: Path) -> str:
    if channel_key == "facebook_post":
        return f"""Use my existing logged-in browser session and publish this post to my personal Facebook profile.
Open Facebook, start a new feed post, paste the content exactly as provided below, and publish it.
Do not rewrite or summarize the content.

CONTENT TO POST:
{text}
"""
    if channel_key == "linkedin_post":
        return f"""Use my existing logged-in browser session and publish this post to my personal LinkedIn profile.
Create a standard LinkedIn feed post, paste the Recommended Post content exactly as provided below, and publish it.
Do not rewrite or summarize the content.

CONTENT TO POST:
{text}
"""
    if channel_key == "linkedin_article":
        return f"""Use my existing logged-in browser session and publish this article on LinkedIn as an article.
Use the provided Title, Subtitle, Article, and Closing Reflection. Paste them into the appropriate LinkedIn article fields and publish.
Do not rewrite or summarize the content.

CONTENT TO POST:
{text}
"""
    if channel_key == "medium_article":
        return f"""Use my existing logged-in browser session and publish this article on Medium.
Use the provided Title, Subtitle, Article, and Final Takeaway. Paste them into the appropriate Medium editor fields and publish.
Do not rewrite or summarize the content.

CONTENT TO POST:
{text}
"""
    if channel_key == "substack_post":
        return f"""Use my existing logged-in browser session and publish this post on Substack.
Use the provided Title, Subtitle, Post, and Postscript. Paste them into the appropriate Substack editor fields and publish.
Do not rewrite or summarize the content.

CONTENT TO POST:
{text}
"""
    if channel_key == "youtube_package":
        video_path = job_dir / "output" / "edited_video" / "lead-magnet.mp4"
        return f"""Use my existing logged-in browser session and upload the video at this local path to YouTube Studio:
{video_path}

Use the provided Recommended Title, Description, Chapter Suggestions, Tags, Pinned Comment, and Thumbnail Text Options as the upload metadata where appropriate.
If the file chooser appears, use the local file path above.
Do not rewrite the content.

CONTENT TO USE:
{text}
"""
    raise ValueError(channel_key)


async def post_with_browser_use(job_dir: Path, env: dict[str, str], requested_channels: list[str]) -> dict[str, Any]:
    if not browser_use_enabled(env):
        return {"status": "skipped", "reason": "BROWSER_USE_ENABLED is false."}

    try:
        from browser_use import Agent, Browser, ChatOpenAI
    except Exception as exc:  # pragma: no cover - dependency is optional in local dev
        return {"status": "failed", "reason": f"browser-use import failed: {exc}"}

    allowed = [item.strip() for item in env.get("BROWSER_USE_CHANNELS", "").split(",") if item.strip()]
    channel_keys = [key for key in requested_channels if key in (allowed or DEFAULT_BROWSER_USE_CHANNELS)]
    if not channel_keys:
        return {"status": "skipped", "reason": "No Browser Use channels selected."}

    if env.get("BROWSER_USE_STORAGE_STATE", "").strip():
        browser = Browser(storage_state=env["BROWSER_USE_STORAGE_STATE"].strip())
    else:
        browser = Browser.from_system_chrome()

    llm = ChatOpenAI(model=env.get("BROWSER_USE_OPENAI_MODEL", "o3"))
    results: list[dict[str, Any]] = []
    try:
        for channel_key in channel_keys:
            text = read_text(job_dir / "output" / "content_pack" / CHANNEL_FILES[channel_key])
            if not text:
                results.append({"channel": channel_key, "status": "skipped", "reason": "Generated content file is missing."})
                continue
            task = browser_task_for(channel_key, text, job_dir)
            agent = Agent(task=task, browser=browser, llm=llm)
            response = await agent.run()
            results.append({"channel": channel_key, "status": "posted", "result": str(response)})
    finally:
        stop = getattr(browser, "stop", None)
        if stop is not None:
            await stop()
    return {"status": "completed", "channels": results}


def build_results(job_dir: Path, requested_channels: list[str]) -> dict[str, Any]:
    manifest = read_json(job_dir / "job.json")
    env = load_env_config()
    results: dict[str, Any] = {
        "job_slug": manifest.get("slug", job_dir.name),
        "job_title": manifest.get("title", job_dir.name),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_channels": requested_channels,
        "channels": {},
    }

    if "newsletter" in requested_channels:
        try:
            results["channels"]["newsletter"] = create_kit_broadcast(job_dir, manifest, env)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            results["channels"]["newsletter"] = {"status": "failed", "reason": f"HTTP {exc.code}", "body": body}
        except Exception as exc:
            results["channels"]["newsletter"] = {"status": "failed", "reason": str(exc)}

    browser_channels = [
        key for key in requested_channels
        if key in {"facebook_post", "linkedin_post", "linkedin_article", "medium_article", "substack_post", "youtube_package"}
    ]
    if browser_channels:
        try:
            results["channels"]["browser_use"] = asyncio.run(post_with_browser_use(job_dir, env, browser_channels))
        except Exception as exc:
            results["channels"]["browser_use"] = {"status": "failed", "reason": str(exc)}

    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir")
    parser.add_argument(
        "--channels",
        nargs="*",
        default=["newsletter", *DEFAULT_BROWSER_USE_CHANNELS],
        choices=sorted(CHANNEL_FILES.keys()),
    )
    args = parser.parse_args()

    job_dir = Path(args.job_dir).resolve()
    results = build_results(job_dir, args.channels)
    output_path = job_dir / "output" / "content_pack" / "distribution-results.json"
    write_json(output_path, results)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
