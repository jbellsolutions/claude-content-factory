#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import mimetypes
import re
import shutil
import subprocess
import textwrap
import uuid
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import reportlab
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from content_pack import generate_content_pack, infer_manifest_fields
from factory_ingest import load_env_config

ROOT = Path(__file__).resolve().parents[1]
REPORTLAB_FONTS = Path(reportlab.__file__).resolve().parent / "fonts"
DISPLAY_FONT_CANDIDATES = [
    "/System/Library/Fonts/NewYork.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    str(REPORTLAB_FONTS / "Vera.ttf"),
]
DISPLAY_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    str(REPORTLAB_FONTS / "VeraBd.ttf"),
]
SANS_FONT_CANDIDATES = [
    "/System/Library/Fonts/Avenir Next.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    str(REPORTLAB_FONTS / "Vera.ttf"),
]

REPLACEMENTS = {
    "Quad Code": "Claude Code",
    "quad code": "Claude Code",
    "Quad Cowork": "Claude Cowork",
    "quad cowork": "Claude Cowork",
    "Quad co-work": "Claude Cowork",
    "Cloud Co-work": "Claude Cowork",
    "Cloud Code": "Claude Code",
    "Cloud Chat": "Claude Chat",
    "Read AI": "Read.ai",
    "Quad Ecosystem": "Claude Ecosystem",
    "Boris Chesney": "Boris Cherny",
    "Boris Chestney": "Boris Cherny",
}

DROP_PATTERNS = [
    re.compile(r"^(alright|alrighty|cool|boom|good stuff|fair enough)[.! ]*$", re.I),
    re.compile(r"^(yes|right|okay|thank you|sure)[.! ]*$", re.I),
]
LEGACY_VOICE_NOTES = {
    "",
    "Authority content. Useful, specific, not salesy, not promotional.",
}


@dataclass
class Cue:
    start: float
    end: float
    text: str


def load_manifest(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text())


def save_manifest(job_dir: Path, manifest: dict) -> None:
    (job_dir / "job.json").write_text(json.dumps(manifest, indent=2))


def to_seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def to_vtt_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def font(paths: str | list[str], size: int) -> ImageFont.ImageFont:
    candidates = [paths] if isinstance(paths, str) else paths
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False))


def parse_vtt(contents: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", contents.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0] == "WEBVTT":
            continue
        if "-->" in lines[0]:
            timing = lines[0]
            text_lines = lines[1:]
        else:
            if len(lines) < 2:
                continue
            timing = lines[1]
            text_lines = lines[2:]
        if "-->" not in timing:
            continue
        start_text, end_text = [part.strip() for part in timing.split("-->")]
        cues.append(Cue(to_seconds(start_text), to_seconds(end_text), " ".join(text_lines)))
    return cues


def normalize_text(text: str) -> str:
    text = re.sub(r"^[A-Za-z ]+:\s*", "", text)
    text = text.replace("…", "...")
    for old, new in REPLACEMENTS.items():
        text = text.replace(old, new)
    text = re.sub(r"\b(um+|uh+|ah+)\b", "", text, flags=re.I)
    text = re.sub(r"\b(?:you know what I'm saying\??)\b", "", text, flags=re.I)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -")


def should_drop(text: str) -> bool:
    if not text:
        return True
    lowered = text.lower().strip()
    if len(lowered.split()) <= 2 and any(pattern.match(lowered) for pattern in DROP_PATTERNS):
        return True
    return False


def cleaned_cues_from_vtt(vtt_path: Path) -> list[Cue]:
    cues = parse_vtt(vtt_path.read_text())
    cleaned = []
    for cue in cues:
        text = normalize_text(cue.text)
        if should_drop(text):
            continue
        cleaned.append(Cue(cue.start, cue.end, text))
    return cleaned


def auto_segments(cues: list[Cue], gap_threshold: float = 1.4) -> list[list[float]]:
    if not cues:
        return []
    segments: list[list[float]] = []
    current_start = cues[0].start
    current_end = cues[0].end
    for cue in cues[1:]:
        if cue.start - current_end <= gap_threshold:
            current_end = cue.end
        else:
            segments.append([current_start, current_end])
            current_start = cue.start
            current_end = cue.end
    segments.append([current_start, current_end])
    return [segment for segment in segments if segment[1] - segment[0] > 1.0]


def selected_cues(cues: list[Cue], segments: list[list[float]]) -> list[Cue]:
    selected: list[Cue] = []
    offset = 0.0
    for segment_start, segment_end in segments:
        for cue in cues:
            if cue.end <= segment_start or cue.start >= segment_end:
                continue
            clipped_start = max(cue.start, segment_start)
            clipped_end = min(cue.end, segment_end)
            selected.append(
                Cue(
                    start=(clipped_start - segment_start) + offset,
                    end=(clipped_end - segment_start) + offset,
                    text=cue.text,
                )
            )
        offset += segment_end - segment_start
    return selected


def write_transcript_outputs(job_dir: Path, cues: list[Cue]) -> tuple[Path, Path]:
    transcript_dir = job_dir / "output" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    txt_path = transcript_dir / "transcript.txt"
    vtt_path = transcript_dir / "captions.vtt"

    paragraphs: list[str] = []
    current: list[str] = []
    previous_end = None
    for cue in cues:
        if previous_end is not None and cue.start - previous_end > 3 and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(cue.text)
        previous_end = cue.end
    if current:
        paragraphs.append(" ".join(current))

    txt_path.write_text("\n\n".join(re.sub(r"\s{2,}", " ", p).strip() for p in paragraphs))

    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        lines.extend([str(index), f"{to_vtt_timestamp(cue.start)} --> {to_vtt_timestamp(cue.end)}", cue.text, ""])
    vtt_path.write_text("\n".join(lines))
    return txt_path, vtt_path


def prepare_audio_for_transcription(ffmpeg: str, media_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(output_path),
        ]
    )
    return output_path


def segment_audio_for_transcription(ffmpeg: str, audio_path: Path, chunk_dir: Path, segment_seconds: int = 1200) -> list[Path]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(audio_path),
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-c",
            "copy",
            str(chunk_dir / "chunk-%03d.mp3"),
        ]
    )
    return sorted(path for path in chunk_dir.glob("chunk-*.mp3") if path.stat().st_size > 0)


def encode_multipart_form(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    line_break = b"\r\n"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}".encode("utf-8"))
        body.extend(line_break)
        body.extend(f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"))
        body.extend(line_break)
        body.extend(line_break)
        body.extend(str(value).encode("utf-8"))
        body.extend(line_break)
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}".encode("utf-8"))
    body.extend(line_break)
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"'.encode("utf-8")
    )
    body.extend(line_break)
    body.extend(f"Content-Type: {mime_type}".encode("utf-8"))
    body.extend(line_break)
    body.extend(line_break)
    body.extend(file_path.read_bytes())
    body.extend(line_break)
    body.extend(f"--{boundary}--".encode("utf-8"))
    body.extend(line_break)
    return bytes(body), boundary


def transcribe_audio_chunk(audio_path: Path, api_key: str, base_url: str, model: str, prompt: str) -> str:
    fields = {
        "model": model,
        "response_format": "text",
    }
    if prompt.strip():
        fields["prompt"] = prompt.strip()
    payload, boundary = encode_multipart_form(fields, "file", audio_path)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/audio/transcriptions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        text = response.read().decode("utf-8").strip()
    if text.startswith("{"):
        try:
            return json.loads(text).get("text", "").strip()
        except json.JSONDecodeError:
            return text
    return text


def generate_transcript_from_media(job_dir: Path, manifest: dict, env: dict) -> Path | None:
    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    source_audio = manifest.get("source_audio")
    source_video = manifest.get("source_video")
    if not source_audio and not source_video:
        return None

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    output_dir = job_dir / "output"
    transcript_dir = output_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = output_dir / ".tmp_transcription"
    temp_dir.mkdir(parents=True, exist_ok=True)

    source_media = job_dir / (source_audio or source_video)
    prepared_audio = prepare_audio_for_transcription(ffmpeg, source_media, temp_dir / "transcription-source.mp3")
    chunks = segment_audio_for_transcription(ffmpeg, prepared_audio, temp_dir / "chunks")
    if not chunks and prepared_audio.exists() and prepared_audio.stat().st_size > 0:
        chunks = [prepared_audio]

    model = env.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
    base_url = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip() or "https://api.openai.com/v1"
    prompt = (
        "Transcribe this business, AI, and software workflow recording cleanly. "
        "Prefer Claude Chat, Claude Cowork, Claude Code, Kit, Substack, Medium, LinkedIn, Facebook, YouTube, and ConvertKit spellings when spoken."
    )
    transcript_parts: list[str] = []
    for chunk in chunks:
        text = transcribe_audio_chunk(chunk, api_key, base_url, model, prompt)
        cleaned = re.sub(r"\s{2,}", " ", text.replace("\r\n", "\n")).strip()
        if cleaned:
            transcript_parts.append(cleaned)
    if not transcript_parts:
        return None

    transcript_path = transcript_dir / "transcript.txt"
    transcript_path.write_text("\n\n".join(transcript_parts).strip())
    return transcript_path


def make_background(width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height), "#f0e8dc")
    draw = ImageDraw.Draw(img)
    for y in range(height):
        blend = y / max(height - 1, 1)
        r = int(240 - (240 - 18) * blend)
        g = int(232 - (232 - 39) * blend)
        b = int(220 - (220 - 48) * blend)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    for box, fill in [
        ((-100, -120, width // 2, height // 2), (234, 127, 58)),
        ((width // 2, -80, width + 180, height // 2 + 120), (40, 83, 92)),
        ((width // 3, height // 2, width + 120, height + 120), (7, 27, 39)),
    ]:
        blob = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        blob_draw = ImageDraw.Draw(blob)
        blob_draw.ellipse(box, fill=fill + (120,))
        blob = blob.filter(ImageFilter.GaussianBlur(70))
        img = Image.alpha_composite(img.convert("RGBA"), blob).convert("RGB")
    return img


def draw_badge(img: Image.Image, center: tuple[int, int], radius: int, top: str, bottom: str) -> None:
    draw = ImageDraw.Draw(img)
    cx, cy = center
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#f8efe1", outline="#153540", width=5)
    draw.ellipse((cx - radius + 16, cy - radius + 16, cx + radius - 16, cy + radius - 16), outline="#ea7f3a", width=4)
    for i in range(18):
        angle = math.radians((360 / 18) * i)
        x1 = cx + math.cos(angle) * (radius - 10)
        y1 = cy + math.sin(angle) * (radius - 10)
        x2 = cx + math.cos(angle) * (radius + 8)
        y2 = cy + math.sin(angle) * (radius + 8)
        draw.line((x1, y1, x2, y2), fill="#ea7f3a", width=3)
    draw.text((cx - 82, cy - 22), top, font=font(DISPLAY_BOLD_CANDIDATES, 26), fill="#153540")
    draw.text((cx - 50, cy + 8), bottom, font=font(SANS_FONT_CANDIDATES, 18), fill="#4f6267")


def render_brand_assets(job_dir: Path, manifest: dict) -> None:
    assets = job_dir / "output" / "site" / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    def render_intro(title: str, body: str, kind: str, path: Path) -> None:
        img = make_background(1280, 720)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((78, 74, 1196, 646), radius=42, fill="#f8efe1", outline="#dbcbb6", width=2)
        draw.text((126, 136), kind, font=font(SANS_FONT_CANDIDATES, 24), fill="#ea7f3a")
        title_text = wrap(title, 18)
        draw.multiline_text((126, 184), title_text, font=font(DISPLAY_FONT_CANDIDATES, 56), fill="#102731", spacing=6)
        title_box = draw.multiline_textbbox((126, 184), title_text, font=font(DISPLAY_FONT_CANDIDATES, 56), spacing=6)
        body_y = title_box[3] + 26
        draw.multiline_text((126, body_y), wrap(body, 34), font=font(SANS_FONT_CANDIDATES, 26), fill="#4f6267", spacing=10)
        draw_badge(img, (1040, 514), 88, "Claude", "Guide")
        img.save(path, quality=95)

    render_intro(
        manifest["title"],
        manifest["headline"],
        "INTRO",
        assets / "intro-slate.png",
    )
    render_intro(
        "Keep Going",
        "Take the full Claude Code Ecosystem Level One Certification free and go deeper on setup, workflow, and execution.",
        "OUTRO",
        assets / "outro-slate.png",
    )

    hero = make_background(1500, 980)
    draw = ImageDraw.Draw(hero)
    draw.rounded_rectangle((72, 76, 962, 892), radius=42, fill="#f8efe1", outline="#dbcbb6", width=2)
    draw.text((124, 138), "LEAD MAGNET", font=font(SANS_FONT_CANDIDATES, 24), fill="#ea7f3a")
    title_text = wrap(manifest["title"], 16)
    draw.multiline_text((124, 184), title_text, font=font(DISPLAY_FONT_CANDIDATES, 78), fill="#102731", spacing=8)
    title_box = draw.multiline_textbbox((124, 184), title_text, font=font(DISPLAY_FONT_CANDIDATES, 78), spacing=8)
    headline_y = title_box[3] + 18
    draw.multiline_text((124, headline_y), wrap(manifest["headline"], 24), font=font(DISPLAY_BOLD_CANDIDATES, 34), fill="#153540", spacing=8)
    headline_box = draw.multiline_textbbox((124, headline_y), wrap(manifest["headline"], 24), font=font(DISPLAY_BOLD_CANDIDATES, 34), spacing=8)
    body_y = headline_box[3] + 20
    draw.multiline_text((124, body_y), wrap(manifest["subheadline"], 38), font=font(SANS_FONT_CANDIDATES, 28), fill="#4f6267", spacing=10)
    draw_badge(hero, (1304, 870), 70, "Claude", "Factory")
    hero.save(assets / "hero-art.png", quality=95)
    hero.save(assets / "social-card.png", quality=95)


def build_still_clip(ffmpeg: str, image_path: Path, output_path: Path, seconds: int = 5) -> None:
    run(
        [
            ffmpeg, "-y", "-loop", "1", "-i", str(image_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", str(seconds), "-r", "30",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-shortest", str(output_path)
        ]
    )


def render_video(job_dir: Path, manifest: dict, segments: list[list[float]]) -> Path:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source_video = job_dir / manifest["source_video"]
    output_dir = job_dir / "output" / "edited_video"
    tmp_dir = output_dir / ".tmp"
    assets = job_dir / "output" / "site" / "assets"
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_clip = output_dir / "lead-magnet.mp4"

    intro = tmp_dir / "intro.mp4"
    outro = tmp_dir / "outro.mp4"
    body = tmp_dir / "body.mp4"
    build_still_clip(ffmpeg, assets / "intro-slate.png", intro)
    build_still_clip(ffmpeg, assets / "outro-slate.png", outro)

    if segments:
        parts = []
        concat_inputs = []
        for index, (start_sec, end_sec) in enumerate(segments):
            parts.append(
                f"[0:v]trim=start={start_sec}:end={end_sec},setpts=PTS-STARTPTS,"
                f"scale=1280:720:force_original_aspect_ratio=decrease,"
                f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p[v{index}]"
            )
            parts.append(
                f"[0:a]atrim=start={start_sec}:end={end_sec},asetpts=PTS-STARTPTS,"
                f"highpass=f=90,lowpass=f=12000,afftdn=nf=-22[a{index}]"
            )
            concat_inputs.append(f"[v{index}][a{index}]")
        parts.append("".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[vtemp][atemp]")
        parts.append("[atemp]loudnorm=I=-16:LRA=11:TP=-1.5[aout]")
        run(
            [
                ffmpeg, "-y", "-i", str(source_video), "-filter_complex", ";".join(parts),
                "-map", "[vtemp]", "-map", "[aout]", "-r", "30",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(body)
            ]
        )
    else:
        run(
            [
                ffmpeg, "-y", "-i", str(source_video),
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-af", "highpass=f=90,lowpass=f=12000,afftdn=nf=-22,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(body)
            ]
        )

    concat_list = tmp_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{part.as_posix()}'\n" for part in [intro, body, outro]))
    run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(final_clip)])
    run([ffmpeg, "-y", "-ss", "00:00:08", "-i", str(final_clip), "-frames:v", "1", "-q:v", "2", str(assets / "poster.jpg")])
    return final_clip


def render_pdf(job_dir: Path, manifest: dict) -> Path:
    output = job_dir / "output" / "deliverables" / "companion-guide.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.75 * inch, bottomMargin=0.75 * inch, title=manifest["pdf_title"])
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("LeadTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=HexColor("#153540"), spaceAfter=12)
    subtitle_style = ParagraphStyle("LeadSubtitle", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=13, leading=18, textColor=HexColor("#ea7f3a"), spaceAfter=12)
    body_style = ParagraphStyle("LeadBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=11, leading=16, textColor=HexColor("#4f6267"), spaceAfter=10)
    section_style = ParagraphStyle("LeadSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=HexColor("#153540"), spaceBefore=12, spaceAfter=8)
    story = [
        Paragraph(manifest["pdf_title"], title_style),
        Paragraph(manifest["headline"], subtitle_style),
        Paragraph(manifest["lead"], body_style),
        Spacer(1, 0.1 * inch),
        Paragraph("Checklist Of What They Learned", section_style),
        ListFlowable([ListItem(Paragraph(item, body_style)) for item in manifest["checklist"]], bulletType="bullet", leftIndent=16),
        Spacer(1, 0.18 * inch),
        Paragraph("Next Step", section_style),
        Paragraph(f"Take the full Claude Code Ecosystem Level One Certification free here:<br/><a href='{manifest['cta_url']}'>{manifest['cta_url']}</a>", body_style),
    ]
    doc.build(story)
    return output


def page_html(manifest: dict, has_captions: bool) -> str:
    form_action = manifest.get("kit_form_action", "").strip()
    button_text = manifest.get("kit_button_text", "Get Access")
    if form_action:
        form_markup = f"""
        <form class="optin-form" action="{form_action}" method="post">
          <input type="email" name="email_address" placeholder="Drop your email here to get access" required />
          <button type="submit">{button_text}</button>
        </form>
        """
    else:
        form_markup = f"""
        <form class="optin-form" action="#" method="post">
          <input type="email" name="email_address" placeholder="Drop your email here to get access" required />
          <button type="submit">{button_text}</button>
        </form>
        <p class="note">Set <code>kit_form_action</code> in the job manifest to wire this to Kit.</p>
        """
    captions = """
              <track kind="captions" src="transcripts/captions.vtt" srclang="en" label="English" default />
    """ if has_captions else ""
    checklist_markup = "\n".join(f"              <li>{item}</li>" for item in manifest["checklist"])
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{manifest['title']}</title>
    <meta name="description" content="{manifest['subheadline']}" />
    <meta property="og:title" content="{manifest['title']}" />
    <meta property="og:description" content="{manifest['headline']}" />
    <meta property="og:image" content="site/assets/social-card.png" />
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <div class="page-shell">
      <header class="topbar">
        <div class="brand-lockup"><span class="brand-mark"></span><div><p class="eyebrow">Lead Magnet</p><p class="brand-name">Claude Content Factory</p></div></div>
        <nav class="nav"><a href="#video">Watch</a><a href="#checklist">Checklist</a><a href="deliverables/companion-guide.pdf" download>Download PDF</a></nav>
      </header>
      <main>
        <section class="hero">
          <div class="hero-copy">
            <p class="eyebrow">Claude Chat + Claude Cowork + Claude Code</p>
            <h1>{manifest['title']}</h1>
            <p class="hero-headline">{manifest['headline']}</p>
            <p class="hero-subheadline">{manifest['subheadline']}</p>
            <p class="hero-lead">{manifest['lead']}</p>
            {form_markup}
          </div>
          <div class="hero-art"><img src="site/assets/hero-art.png" alt="{manifest['title']} artwork" /></div>
        </section>
        <section class="video-section" id="video">
          <div class="section-header"><p class="section-kicker">Video</p><h2>Watch the edited lead magnet.</h2></div>
          <div class="video-card">
            <video controls preload="metadata" poster="site/assets/poster.jpg">
              <source src="edited_video/lead-magnet.mp4" type="video/mp4" />
{captions}            </video>
            <div class="download-row">
              <a class="button button-secondary" href="deliverables/companion-guide.pdf" download>Download Companion PDF</a>
              <a class="button button-secondary" href="transcripts/transcript.txt" download>Download Transcript</a>
            </div>
          </div>
        </section>
        <section class="checklist-section" id="checklist">
          <div class="section-header"><p class="section-kicker">Checklist</p><h2>What they learned.</h2></div>
          <div class="checklist-card"><ul class="checklist-list">
{checklist_markup}
          </ul></div>
        </section>
        <section class="cta-section">
          <p class="section-kicker">Next Step</p>
          <h2>Get your full level one certification here for free.</h2>
          <p>If you want the deeper Claude Code ecosystem walkthrough, go here next.</p>
          <a class="button button-primary" href="{manifest['cta_url']}" target="_blank" rel="noreferrer">{manifest['cta_label']}</a>
        </section>
      </main>
    </div>
  </body>
</html>"""


def page_css() -> str:
    return """
:root{--surface:rgba(248,239,225,.84);--ink:#153540;--muted:#54676c;--line:rgba(21,53,64,.12);--accent:#ea7f3a;--shadow:0 30px 80px rgba(4,20,29,.24)}
*{box-sizing:border-box}body{margin:0;font-family:"Avenir Next","Segoe UI",sans-serif;color:var(--ink);background:radial-gradient(circle at top left,rgba(234,127,58,.34),transparent 24%),radial-gradient(circle at top right,rgba(117,152,159,.24),transparent 24%),linear-gradient(180deg,#d0b38f 0%,#173441 42%,#081721 100%);min-height:100vh}img,video{display:block;max-width:100%}a{text-decoration:none;color:inherit}
.page-shell{width:min(1180px,calc(100vw - 28px));margin:0 auto;padding:22px 0 42px}.topbar,.hero,.video-section,.checklist-section,.cta-section{position:relative;z-index:1}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:18px 24px;border-radius:999px;background:rgba(8,23,33,.52);color:#f8efe1;border:1px solid rgba(255,255,255,.18);box-shadow:var(--shadow);backdrop-filter:blur(18px)}
.brand-lockup{display:flex;align-items:center;gap:14px}.brand-mark{width:16px;height:16px;border-radius:999px;background:linear-gradient(135deg,#f7c38a,var(--accent));box-shadow:0 0 0 6px rgba(234,127,58,.18)}.eyebrow,.section-kicker{margin:0 0 8px;letter-spacing:.08em;text-transform:uppercase;font-size:.78rem;font-weight:800;color:var(--accent)}.brand-name{margin:0;font-weight:600}.nav{display:flex;gap:18px;flex-wrap:wrap;color:rgba(248,239,225,.84)}
.hero,.video-section,.checklist-section,.cta-section{margin-top:24px;border-radius:34px;background:var(--surface);border:1px solid rgba(255,255,255,.34);box-shadow:var(--shadow);backdrop-filter:blur(18px)}
.hero{display:grid;grid-template-columns:1.05fr .95fr;gap:28px;align-items:center;padding:30px}.hero-copy h1,.section-header h2,.cta-section h2{font-family:"New York","Iowan Old Style",Georgia,serif}.hero-copy h1{margin:0;font-size:clamp(3rem,6vw,5.2rem);line-height:.94;max-width:10ch}.hero-headline{margin:18px 0 0;font-size:clamp(1.6rem,3vw,2.4rem);line-height:1.06;font-family:"Iowan Old Style",Georgia,serif}.hero-subheadline,.hero-lead,.section-header p,.cta-section p,.checklist-list,.note{color:var(--muted);line-height:1.65}
.optin-form{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}.optin-form input{flex:1 1 280px;min-height:50px;border-radius:999px;border:1px solid var(--line);padding:0 18px;font:inherit}.optin-form button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:50px;padding:0 22px;border-radius:999px;font-weight:800;border:0}.optin-form button,.button-primary{background:var(--ink);color:#fff8ef}.button-secondary{background:rgba(255,255,255,.56);color:var(--ink);border:1px solid var(--line)}.hero-art img{width:100%;border-radius:28px;box-shadow:0 24px 60px rgba(10,26,34,.28)}
.video-section,.checklist-section,.cta-section{padding:30px}.section-header{max-width:50rem}.section-header h2{margin:0 0 10px;font-size:clamp(2rem,4vw,3.2rem);line-height:.98}.section-header p{margin:0}.video-card,.checklist-card{margin-top:24px;padding:18px;border-radius:28px;background:rgba(255,248,239,.72);border:1px solid rgba(255,255,255,.34)}.video-card video{width:100%;border-radius:22px;background:#0b1d27}.download-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}.checklist-list{margin:0;padding-left:22px;font-size:1.03rem}.checklist-list li+li{margin-top:10px}.cta-section{text-align:center}.cta-section p{max-width:44rem;margin:0 auto 22px}
@media (max-width:980px){.topbar,.hero{grid-template-columns:1fr}.hero{padding:24px}.hero-copy h1{max-width:12ch}}
"""


def render_page(job_dir: Path, manifest: dict, has_captions: bool) -> None:
    output = job_dir / "output"
    (output / "index.html").write_text(page_html(manifest, has_captions))
    (output / "styles.css").write_text(page_css())


def build_job(job_dir: Path) -> None:
    manifest = load_manifest(job_dir)
    env = load_env_config()
    default_voice_notes = env.get(
        "VOICE_NOTES",
        "Direct, tactical, founder-led, authority-building, clear, high-agency, and useful. Sound like a real operator. Not salesy, not promotional, not generic. Use light platform-native emojis where natural.",
    )
    if str(manifest.get("voice_notes", "")).strip() in LEGACY_VOICE_NOTES:
        manifest["voice_notes"] = default_voice_notes
    output_dir = job_dir / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cues: list[Cue] = []
    segments: list[list[float]] = []
    has_captions = False
    transcript_path = None
    if manifest.get("source_vtt"):
        cues = cleaned_cues_from_vtt(job_dir / manifest["source_vtt"])
        if manifest.get("manual_segments"):
            segments = [[to_seconds(start), to_seconds(end)] for start, end in manifest["manual_segments"]]
        else:
            segments = auto_segments(cues)
        selected = selected_cues(cues, segments) if segments else cues
        transcript_path, _ = write_transcript_outputs(job_dir, selected)
        has_captions = True
    elif manifest.get("source_text"):
        transcript_dir = output_dir / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / "transcript.txt"
        transcript_path.write_text((job_dir / manifest["source_text"]).read_text().strip())
    else:
        transcript_path = generate_transcript_from_media(job_dir, manifest, env)

    if transcript_path and transcript_path.exists():
        manifest = infer_manifest_fields(manifest, transcript_path.read_text())
        save_manifest(job_dir, manifest)
    render_brand_assets(job_dir, manifest)
    render_video(job_dir, manifest, segments)
    render_pdf(job_dir, manifest)
    render_page(job_dir, manifest, has_captions)
    generate_content_pack(job_dir, manifest, transcript_path)
    (output_dir / ".nojekyll").write_text("")
    print(f"Built {job_dir.name}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir")
    args = parser.parse_args()
    build_job(Path(args.job_dir).resolve())


if __name__ == "__main__":
    main()
