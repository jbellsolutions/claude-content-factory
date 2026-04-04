#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

from factory_ingest import load_env_config
from runtime_paths import CODE_ROOT


DNA_PATH = CODE_ROOT / "content_dna" / "authority_council.json"
BRAND_REPLACEMENTS = {
    "Quad Code": "Claude Code",
    "quad code": "Claude Code",
    "Cloud Code": "Claude Code",
    "cloud code": "Claude Code",
    "Quad Cowork": "Claude Cowork",
    "quad cowork": "Claude Cowork",
    "Quad Co-Work": "Claude Cowork",
    "quad co-work": "Claude Cowork",
    "Cloud Cowork": "Claude Cowork",
    "cloud cowork": "Claude Cowork",
    "Cloud Co-Work": "Claude Cowork",
    "cloud co-work": "Claude Cowork",
    "Cloud Chat": "Claude Chat",
    "cloud chat": "Claude Chat",
    "Quad Content Factory": "Claude Content Factory",
    "quad content factory": "Claude Content Factory",
    "Quad Ecosystem": "Claude Ecosystem",
    "quad ecosystem": "Claude Ecosystem",
}


def load_dna() -> dict:
    return json.loads(DNA_PATH.read_text())


def trimmed_transcript(text: str, max_chars: int = 32000) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2].rstrip()
    tail = text[-(max_chars // 2):].lstrip()
    return head + "\n\n[... transcript truncated for prompt size ...]\n\n" + tail


def write_readme(output_dir: Path, manifest: dict, generated_files: list[str], note: str) -> None:
    listing = "\n".join(f"- `{name}`" for name in generated_files) if generated_files else "- No generated files yet"
    output_dir.joinpath("README.md").write_text(
        "\n".join(
            [
                f"# {manifest['title']} Content Pack",
                "",
                note,
                "",
                "## Files",
                listing,
            ]
        )
    )


def normalize_ready_to_post_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("```", "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return normalize_brand_text(text.strip())


def normalize_brand_text(text: str) -> str:
    normalized = text
    for old, new in BRAND_REPLACEMENTS.items():
        normalized = normalized.replace(old, new)
    return normalized.replace("Co-Work", "Cowork").replace("co-work", "cowork")


def extract_json_object(text: str) -> dict:
    fenced = re.search(r"```json\s*(\{.*\})\s*```", text, flags=re.S)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output")
    raw = candidate[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", raw)
        return json.loads(cleaned)


def call_openai(prompt: str, system_prompt: str, model: str, api_key: str, base_url: str) -> str:
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.loads(response.read().decode("utf-8"))
    parts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()


def authority_system_prompt(dna: dict) -> str:
    lines = [
        f"You are {dna['name']}.",
        dna["positioning"],
        "",
        "Global rules:",
    ]
    lines.extend(f"- {rule}" for rule in dna["global_rules"])
    lines.extend(
        [
            "",
            "Always stay grounded in the source transcript. If a detail is not clearly in the source, leave it out.",
            "Favor specificity, clean structure, and authority. Avoid fake urgency and marketing theatrics.",
        ]
    )
    return "\n".join(lines)


def voice_context(dna: dict, channel_key: str) -> str:
    selected = [voice for voice in dna["core_voices"] if channel_key in voice["use_for"] or "authority_content" in voice["use_for"]]
    lines = ["Voice blend to reference:"]
    for voice in selected:
        traits = ", ".join(voice["style_traits"])
        lines.append(f"- {voice['display_name']}: {traits}")
    brief = dna["channel_briefs"][channel_key]
    lines.extend(
        [
            "",
            f"Channel goal: {brief['goal']}",
            f"Preferred format: {brief['format']}",
        ]
    )
    return "\n".join(lines)


def base_context(manifest: dict, brief_text: str) -> str:
    checklist = "\n".join(f"- {item}" for item in manifest.get("checklist", [])) or "- None provided"
    return textwrap.dedent(
        f"""
        Project title: {manifest['title']}
        Headline: {manifest.get('headline', '')}
        Subheadline: {manifest.get('subheadline', '')}
        Lead: {manifest.get('lead', '')}
        Brand name: {manifest.get('brand_name', manifest['title'])}
        Audience: {manifest.get('target_audience', 'Founders, operators, executives, employees learning AI')}
        Voice notes: {manifest.get('voice_notes', 'Direct, tactical, founder-led, authority-building, clear, high-agency, and useful. Sound like a real operator. Not salesy, not promotional, not generic. Use light platform-native emojis where natural.')}
        CTA URL: {manifest.get('cta_url', '')}

        Existing checklist:
        {checklist}

        Authority brief:
        {brief_text}
        """
    ).strip()


def generate_brief(transcript_text: str, manifest: dict, dna: dict, api_key: str, model: str, base_url: str) -> str:
    prompt = textwrap.dedent(
        f"""
        Build an authority-content brief from the source transcript below.

        Requirements:
        - Write in markdown.
        - Cover: executive summary, audience fit, 8-15 major topics, 8-12 memorable quotes or paraphrased ideas, 5 strong hooks, 5 article angles, 5 newsletter angles, and 5 practical takeaways.
        - If the transcript includes clear chapter breaks or transitions, suggest YouTube chapter titles.
        - Do not make anything up.
        - Do not write promotional copy.

        Source transcript:
        {trimmed_transcript(transcript_text)}
        """
    ).strip()
    return call_openai(prompt, authority_system_prompt(dna), model, api_key, base_url)


def infer_manifest_fields(manifest: dict, transcript_text: str) -> dict:
    requested_fields = set(manifest.get("auto_fill_fields", []))
    if not transcript_text.strip():
        return manifest

    missing_by_value = {
        field
        for field in ["title", "headline", "subheadline", "lead", "checklist", "target_audience", "brand_name"]
        if not manifest.get(field)
    }
    fields_to_fill = sorted(requested_fields | missing_by_value)
    if not fields_to_fill:
        return manifest

    env = load_env_config()
    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        return manifest

    model = env.get("OPENAI_MODEL", "gpt-5")
    base_url = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    prompt = textwrap.dedent(
        f"""
        Fill the requested page metadata fields from the transcript below.

        Requested fields: {", ".join(fields_to_fill)}

        Requirements:
        - Return JSON only.
        - Keep it grounded in the transcript.
        - Do not use hype or promotional language.
        - Make the result strong enough for a polished lead magnet page.
        - `checklist` must be an array of 4 to 7 concise bullets.
        - If `brand_name` is not obvious from the transcript, return an empty string for it.
        - If `target_audience` is not obvious, infer the most likely serious audience from the transcript.

        Current manifest context:
        {json.dumps({k: manifest.get(k) for k in ['title', 'headline', 'subheadline', 'lead', 'brand_name', 'target_audience']}, indent=2)}

        Transcript:
        {trimmed_transcript(transcript_text)}
        """
    ).strip()
    try:
        response_text = call_openai(prompt, authority_system_prompt(load_dna()), model, api_key, base_url)
        payload = extract_json_object(response_text)
    except Exception:
        return manifest
    updated = dict(manifest)
    for field in fields_to_fill:
        if field not in payload:
            continue
        value = payload[field]
        if field == "checklist":
            if isinstance(value, list):
                cleaned = [str(item).strip() for item in value if str(item).strip()]
                if cleaned:
                    updated[field] = cleaned
        elif isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                updated[field] = normalize_brand_text(cleaned)
    return updated


def channel_prompt(channel_key: str, manifest: dict, brief_text: str, dna: dict) -> str:
    shared = base_context(manifest, brief_text)
    if channel_key == "facebook_post":
        task = """
        Create one Facebook post in ready-to-paste plain text.
        Use natural emojis only where they improve readability.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Post Title:
        Ready-To-Post Copy:
        Image Caption:
        Constraints:
        - 180 to 350 words
        - clear, helpful, human
        - no hashtags unless they are clearly useful
        """
    elif channel_key == "linkedin_post":
        task = """
        Create one LinkedIn post in ready-to-paste plain text.
        Use line breaks intentionally so it reads like a published LinkedIn post.
        Use 1-3 tasteful emojis only if they genuinely improve the post.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Hook Options:
        Recommended Post:
        Constraints:
        - concise and strategic
        - avoid sounding like ad copy
        - no emoji-heavy formatting
        """
    elif channel_key == "linkedin_article":
        task = """
        Create one LinkedIn article in ready-to-paste plain text for the LinkedIn article editor.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Title:
        Subtitle:
        Article:
        Closing Reflection:
        Constraints:
        - 900 to 1400 words
        - thought leadership tone
        - grounded in the source material
        """
    elif channel_key == "medium_article":
        task = """
        Create one Medium article in ready-to-paste plain text for the Medium editor.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Title:
        Subtitle:
        Article:
        Final Takeaway:
        Constraints:
        - 1100 to 1700 words
        - insight-rich and readable
        - not platform-jargon heavy
        """
    elif channel_key == "substack_post":
        task = """
        Create one Substack post in ready-to-paste plain text for the Substack editor.
        Make it feel like a credible, human essay-newsletter hybrid.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Title:
        Subtitle:
        Post:
        Postscript:
        Constraints:
        - 900 to 1500 words
        - personal without being casual or flimsy
        - authority-building, not promotional
        """
    elif channel_key == "newsletter":
        task = """
        Create one ConvertKit-ready newsletter in plain text.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Subject Options:
        Recommended Subject:
        Preview Text:
        Newsletter Body:
        Soft CTA:
        Constraints:
        - one big idea
        - 500 to 800 words
        - useful and authority-building, not promotional
        """
    elif channel_key == "youtube_package":
        task = """
        Create a YouTube publishing package in ready-to-use plain text.
        Do not use markdown syntax, bullets with `-`, code fences, or backticks.
        Return these labeled sections exactly:
        Title Options:
        Recommended Title:
        Description:
        Chapter Suggestions:
        Tags:
        Pinned Comment:
        Thumbnail Text Options:
        Constraints:
        - no clickbait
        - make the package honest and publish-ready
        """
    else:
        raise ValueError(channel_key)
    return "\n\n".join([voice_context(dna, channel_key), shared, textwrap.dedent(task).strip()])


def generate_content_pack(job_dir: Path, manifest: dict, transcript_path: Path | None) -> None:
    output_dir = job_dir / "output" / "content_pack"
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt_note = "This folder contains transcript-derived, ready-to-post authority content generated from the Titan-style content council prompts."
    generated_files: list[str] = []

    transcript_text = transcript_path.read_text().strip() if transcript_path and transcript_path.exists() else ""
    context_path = output_dir / "source-context.md"
    context_path.write_text(
        "\n".join(
            [
                f"# {manifest['title']} Source Context",
                "",
                f"Headline: {manifest.get('headline', '')}",
                f"Subheadline: {manifest.get('subheadline', '')}",
                f"Voice notes: {manifest.get('voice_notes', 'Direct, tactical, founder-led, authority-building, clear, high-agency, and useful. Sound like a real operator. Not salesy, not promotional, not generic. Use light platform-native emojis where natural.')}",
                "",
                "## Transcript Excerpt",
                trimmed_transcript(transcript_text) if transcript_text else "No transcript available.",
            ]
        )
    )
    generated_files.append("source-context.md")

    if not transcript_text:
        write_readme(output_dir, manifest, generated_files, "No content pack was generated because this job does not have a transcript.")
        return

    env = load_env_config()
    if str(manifest.get("generate_content_pack", "true")).lower() not in {"1", "true", "yes"}:
        write_readme(output_dir, manifest, generated_files, "Content-pack generation is disabled for this job.")
        return

    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        write_readme(output_dir, manifest, generated_files, "Set `OPENAI_API_KEY` to generate the full content pack automatically.")
        return

    dna = load_dna()
    model = env.get("OPENAI_MODEL", "gpt-5")
    base_url = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    try:
        brief_text = generate_brief(transcript_text, manifest, dna, api_key, model, base_url)
        output_dir.joinpath("authority-brief.md").write_text(brief_text)
        generated_files.append("authority-brief.md")

        channels = [
            ("facebook_post", "facebook-post.md"),
            ("linkedin_post", "linkedin-post.md"),
            ("linkedin_article", "linkedin-article.md"),
            ("medium_article", "medium-article.md"),
            ("substack_post", "substack-post.md"),
            ("newsletter", "newsletter.md"),
            ("youtube_package", "youtube-package.md"),
        ]
        for channel_key, filename in channels:
            text = normalize_ready_to_post_text(call_openai(
                channel_prompt(channel_key, manifest, brief_text, dna),
                authority_system_prompt(dna),
                model,
                api_key,
                base_url,
            ))
            output_dir.joinpath(filename).write_text(text)
            generated_files.append(filename)
        write_readme(output_dir, manifest, generated_files, prompt_note)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        output_dir.joinpath("generation-error.md").write_text(
            f"# Content Pack Generation Error\n\nHTTP {exc.code}\n\n```\n{body}\n```"
        )
        generated_files.append("generation-error.md")
        write_readme(output_dir, manifest, generated_files, "The content-pack request reached the model provider but failed. See `generation-error.md`.")
    except Exception as exc:
        output_dir.joinpath("generation-error.md").write_text(f"# Content Pack Generation Error\n\n{exc}\n")
        generated_files.append("generation-error.md")
        write_readme(output_dir, manifest, generated_files, "The content-pack generation step failed before completion. See `generation-error.md`.")
