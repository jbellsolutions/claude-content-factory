#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


FILLER_WORDS = {
    "um",
    "uh",
    "ah",
    "erm",
    "hmm",
    "mm",
    "mhm",
    "youknow",
    "like",
    "basically",
    "actually",
    "literally",
    "right",
    "okay",
    "ok",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
    "this",
    "we",
    "you",
    "they",
    "i",
    "or",
    "if",
    "so",
    "but",
    "just",
    "can",
    "kind",
    "sort",
    "really",
    "very",
}

KEYWORDS = {
    "ai",
    "content",
    "factory",
    "tools",
    "workflow",
    "system",
    "prompt",
    "automation",
    "youtube",
    "video",
    "editing",
    "process",
    "quality",
    "agent",
}


@dataclass
class Paths:
    root: Path
    source: Path
    audio: Path
    transcript_json: Path
    cuts_json: Path
    cuts_csv: Path
    keep_json: Path
    rough_filter: Path
    rough_video: Path
    intro_video: Path
    outro_video: Path
    polished_body: Path
    review_video: Path
    final_video: Path
    caption_json: Path
    timeline_map_json: Path
    cards_dir: Path
    transcript_cache_dir: Path


def run(cmd: Sequence[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text())


def save_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def ffprobe_duration(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dirs(paths: Paths) -> None:
    for p in [
        paths.audio.parent,
        paths.transcript_json.parent,
        paths.cuts_json.parent,
        paths.rough_filter.parent,
        paths.rough_video.parent,
        paths.review_video.parent,
        paths.final_video.parent,
        paths.caption_json.parent,
        paths.timeline_map_json.parent,
        paths.cards_dir,
        paths.transcript_cache_dir,
    ]:
        p.mkdir(parents=True, exist_ok=True)


def normalize_token(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def tokenize_for_score(text: str) -> List[str]:
    return [normalize_token(w) for w in text.split() if normalize_token(w)]


def merge_intervals(intervals: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    items = sorted((max(0.0, s), max(0.0, e)) for s, e in intervals if e > s)
    if not items:
        return []
    merged: List[Tuple[float, float]] = [items[0]]
    for s, e in items[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e + 0.03:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


def invert_intervals(total_duration: float, removed: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    removed = merge_intervals(removed)
    keep: List[Tuple[float, float]] = []
    cursor = 0.0
    for s, e in removed:
        s = max(0.0, min(total_duration, s))
        e = max(0.0, min(total_duration, e))
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_duration:
        keep.append((cursor, total_duration))
    return keep


def keep_duration(keep: Sequence[Tuple[float, float]]) -> float:
    return sum(e - s for s, e in keep)


def compact_keep_intervals(keep: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not keep:
        return []
    compacted: List[Tuple[float, float]] = []
    for s, e in keep:
        if e - s < 0.45:
            continue
        if compacted and s - compacted[-1][1] < 0.15:
            compacted[-1] = (compacted[-1][0], e)
        else:
            compacted.append((s, e))
    return compacted


def extract_audio(source: Path, wav_out: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-vn",
            str(wav_out),
        ]
    )


def transcribe(paths: Paths, model_size: str, language: str) -> None:
    print("Transcribing with faster-whisper...")
    from faster_whisper import WhisperModel

    source_hash = file_sha256(paths.source)
    safe_model = re.sub(r"[^a-zA-Z0-9_.-]", "_", model_size)
    cache_path = paths.transcript_cache_dir / f"{source_hash}_{safe_model}_{language}.json"
    if cache_path.exists():
        shutil.copy2(cache_path, paths.transcript_json)
        print(f"Reused transcript cache: {cache_path}")
        print(f"Wrote transcript: {paths.transcript_json}")
        return

    extract_audio(paths.source, paths.audio)

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(paths.audio),
        language=language,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )

    serial_segments: List[Dict] = []
    all_words: List[Dict] = []
    for seg in segments:
        words = []
        if seg.words:
            for w in seg.words:
                if w.start is None or w.end is None:
                    continue
                wd = {
                    "start": float(w.start),
                    "end": float(w.end),
                    "word": w.word.strip(),
                    "probability": float(w.probability or 0.0),
                }
                words.append(wd)
                all_words.append(wd)
        serial_segments.append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text.strip(),
                "words": words,
            }
        )

    all_words = sorted(all_words, key=lambda x: x["start"])
    data = {
        "meta": {
            "language": info.language,
            "language_probability": float(info.language_probability),
            "model_size": model_size,
            "source_sha256": source_hash,
        },
        "segments": serial_segments,
        "words": all_words,
    }
    save_json(paths.transcript_json, data)
    shutil.copy2(paths.transcript_json, cache_path)
    print(f"Wrote transcript: {paths.transcript_json}")


def sentence_chunks(words: Sequence[Dict]) -> List[Dict]:
    chunks: List[Dict] = []
    if not words:
        return chunks

    cur: List[Dict] = [words[0]]
    for i in range(1, len(words)):
        prev = words[i - 1]
        w = words[i]
        gap = w["start"] - prev["end"]
        prev_word = prev["word"].strip()
        end_punct = prev_word.endswith((".", "?", "!", ";"))
        cur_dur = w["end"] - cur[0]["start"]
        if gap > 0.85 or end_punct or cur_dur >= 14.0:
            text = " ".join(x["word"] for x in cur).strip()
            chunks.append(
                {
                    "start": cur[0]["start"],
                    "end": cur[-1]["end"],
                    "text": text,
                }
            )
            cur = [w]
        else:
            cur.append(w)
    text = " ".join(x["word"] for x in cur).strip()
    chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text})
    return chunks


def chunk_score(chunk: Dict) -> float:
    text = chunk["text"]
    duration = max(0.2, chunk["end"] - chunk["start"])
    toks = tokenize_for_score(text)
    if not toks:
        return -1.0
    content = [t for t in toks if t not in STOPWORDS and t not in FILLER_WORDS and len(t) > 2]
    unique_content = len(set(content))
    density = unique_content / duration
    keyword_hits = sum(1 for t in toks if t in KEYWORDS)
    filler_hits = sum(1 for t in toks if t in FILLER_WORDS)
    repeat_penalty = 0.0
    for i in range(1, len(toks)):
        if toks[i] == toks[i - 1]:
            repeat_penalty += 0.2
    long_penalty = max(0.0, duration - 12.0) * 0.08
    return (density * 3.0) + (keyword_hits * 0.6) - (filler_hits * 0.65) - repeat_penalty - long_penalty


def detect_cuts(paths: Paths, target_min: float, target_goal: float, target_max: float) -> None:
    data = load_json(paths.transcript_json)
    words = data.get("words", [])
    duration = ffprobe_duration(paths.source)
    if not words:
        keep = [(0.0, duration)] if duration > 0 else []
        decisions = [
            {
                "type": "no_speech_passthrough",
                "start": 0.0,
                "end": round(duration, 3),
                "duration": round(duration, 3),
                "score": "",
                "text": "No transcript words detected. Keeping full source timeline.",
            }
        ]
        timeline = [
            {
                "src_start": 0.0,
                "src_end": round(duration, 3),
                "dst_start": 0.0,
                "dst_end": round(duration, 3),
            }
        ] if duration > 0 else []

        save_json(
            paths.cuts_json,
            {
                "source_duration": round(duration, 3),
                "keep_duration": round(duration, 3),
                "target_min": target_min,
                "target_goal": target_goal,
                "target_max": target_max,
                "keep_intervals": [{"start": round(s, 3), "end": round(e, 3)} for s, e in keep],
                "removed_intervals": [],
                "decisions": decisions,
                "scored_chunks": [],
            },
        )
        save_json(paths.keep_json, {"keep_intervals": [{"start": s, "end": e} for s, e in keep]})
        save_json(paths.timeline_map_json, {"timeline": timeline})
        with paths.cuts_csv.open("w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["type", "start", "end", "duration", "score", "text"]
            )
            writer.writeheader()
            for d in decisions:
                writer.writerow(d)
        print("Warning: transcript contained no words; using full-length passthrough edit.")
        print(f"Wrote cuts: {paths.cuts_json}")
        print(f"Wrote cut log CSV: {paths.cuts_csv}")
        print(f"Estimated rough duration: {duration / 60:.2f} minutes")
        return

    chunks = sentence_chunks(words)

    removed: List[Tuple[float, float]] = []
    decisions: List[Dict] = []

    for w in words:
        token = normalize_token(w["word"])
        wdur = w["end"] - w["start"]
        if token in FILLER_WORDS and wdur <= 0.75:
            s = max(0.0, w["start"] - 0.08)
            e = min(duration, w["end"] + 0.12)
            removed.append((s, e))
            decisions.append(
                {
                    "type": "filler_word",
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(e - s, 3),
                    "text": w["word"],
                }
            )

    for i in range(1, len(words)):
        prev = words[i - 1]
        cur = words[i]
        gap = cur["start"] - prev["end"]
        if gap > 1.2:
            s = prev["end"] + 0.22
            e = cur["start"] - 0.22
            if e - s > 0.25:
                removed.append((s, e))
                decisions.append(
                    {
                        "type": "long_pause",
                        "start": round(s, 3),
                        "end": round(e, 3),
                        "duration": round(e - s, 3),
                        "text": "",
                    }
                )

    scored_chunks: List[Dict] = []
    for ch in chunks:
        score = chunk_score(ch)
        ch = {**ch, "score": score, "duration": round(ch["end"] - ch["start"], 3)}
        scored_chunks.append(ch)
        in_protected_zone = ch["start"] < 25.0 or ch["end"] > (duration - 20.0)
        low_value = score < 0.30 and 3.0 <= (ch["end"] - ch["start"]) <= 22.0
        if low_value and not in_protected_zone:
            s = max(0.0, ch["start"] - 0.05)
            e = min(duration, ch["end"] + 0.05)
            removed.append((s, e))
            decisions.append(
                {
                    "type": "low_value_chunk",
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(e - s, 3),
                    "text": ch["text"][:180],
                    "score": round(score, 3),
                }
            )

    removed = merge_intervals(removed)
    keep = compact_keep_intervals(invert_intervals(duration, removed))

    cur_keep_duration = keep_duration(keep)
    if cur_keep_duration > target_max:
        median_score = sorted(c["score"] for c in scored_chunks)[len(scored_chunks) // 2]
        candidates = sorted(
            (
                c
                for c in scored_chunks
                if c["start"] > 25.0
                and c["end"] < (duration - 20.0)
                and 1.8 <= (c["end"] - c["start"]) <= 20.0
            ),
            key=lambda c: (c["score"], -c["duration"]),
        )
        for c in candidates:
            if cur_keep_duration <= target_goal:
                break
            # Once we're near the max window, only remove below-median chunks.
            if cur_keep_duration <= target_max and c["score"] >= median_score:
                continue
            s = max(0.0, c["start"] - 0.05)
            e = min(duration, c["end"] + 0.05)
            trial_removed = merge_intervals(removed + [(s, e)])
            trial_keep = compact_keep_intervals(invert_intervals(duration, trial_removed))
            trial_duration = keep_duration(trial_keep)
            if trial_duration < target_min:
                continue
            removed = trial_removed
            keep = trial_keep
            cur_keep_duration = trial_duration
            decisions.append(
                {
                    "type": "target_trim",
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(e - s, 3),
                    "text": c["text"][:180],
                    "score": round(c["score"], 3),
                }
            )

    keep = compact_keep_intervals(keep)

    timeline = []
    cursor = 0.0
    for s, e in keep:
        seg_len = e - s
        timeline.append(
            {
                "src_start": round(s, 3),
                "src_end": round(e, 3),
                "dst_start": round(cursor, 3),
                "dst_end": round(cursor + seg_len, 3),
            }
        )
        cursor += seg_len

    save_json(
        paths.cuts_json,
        {
            "source_duration": round(duration, 3),
            "keep_duration": round(keep_duration(keep), 3),
            "target_min": target_min,
            "target_goal": target_goal,
            "target_max": target_max,
            "keep_intervals": [{"start": round(s, 3), "end": round(e, 3)} for s, e in keep],
            "removed_intervals": [{"start": round(s, 3), "end": round(e, 3)} for s, e in removed],
            "decisions": decisions,
            "scored_chunks": scored_chunks,
        },
    )

    save_json(paths.keep_json, {"keep_intervals": [{"start": s, "end": e} for s, e in keep]})
    save_json(paths.timeline_map_json, {"timeline": timeline})

    with paths.cuts_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["type", "start", "end", "duration", "score", "text"]
        )
        writer.writeheader()
        for d in decisions:
            writer.writerow(
                {
                    "type": d.get("type", ""),
                    "start": d.get("start", ""),
                    "end": d.get("end", ""),
                    "duration": d.get("duration", ""),
                    "score": d.get("score", ""),
                    "text": d.get("text", ""),
                }
            )

    print(f"Wrote cuts: {paths.cuts_json}")
    print(f"Wrote cut log CSV: {paths.cuts_csv}")
    print(f"Estimated rough duration: {keep_duration(keep) / 60:.2f} minutes")


def build_filter_script(keep: Sequence[Tuple[float, float]]) -> str:
    lines: List[str] = []
    for i, (s, e) in enumerate(keep):
        lines.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];")
        lines.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}];")
    interleaved = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
    lines.append(f"{interleaved}concat=n={len(keep)}:v=1:a=1[v][a]")
    return "\n".join(lines) + "\n"


def assemble_rough(paths: Paths) -> None:
    cuts = load_json(paths.cuts_json)
    keep = [(float(i["start"]), float(i["end"])) for i in cuts["keep_intervals"]]
    if not keep:
        raise RuntimeError("No keep intervals available.")

    script = build_filter_script(keep)
    paths.rough_filter.write_text(script)

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(paths.source),
            "-filter_complex_script",
            str(paths.rough_filter),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(paths.rough_video),
        ]
    )
    run(["ffmpeg", "-y", "-i", str(paths.rough_video), "-c", "copy", str(paths.review_video)])
    print(f"Wrote rough cut: {paths.rough_video}")
    print(f"Wrote rough review export: {paths.review_video}")


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            ]
        )
    else:
        candidates.extend(
            [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            ]
        )
    for fp in candidates:
        try:
            if Path(fp).exists():
                return ImageFont.truetype(fp, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_text(draw, text: str, font, max_width: int) -> List[str]:
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for w in words[1:]:
        trial = f"{current} {w}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def render_card_image(path: Path, bg_rgb: Tuple[int, int, int], lines: Sequence[Dict]) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1920, 1080), bg_rgb)
    draw = ImageDraw.Draw(img)
    for spec in lines:
        text = spec["text"]
        size = spec["size"]
        y = spec["y"]
        color = spec["color"]
        font = load_font(size=size, bold=spec.get("bold", False))
        max_width = spec.get("max_width", 1600)
        wrapped = wrap_text(draw, text, font=font, max_width=max_width)
        line_height = spec.get("line_height", int(size * 1.22))
        total_h = line_height * len(wrapped)
        y0 = int(y - total_h / 2)
        for idx, line in enumerate(wrapped):
            bbox = draw.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
            draw.text(((1920 - w) / 2, y0 + idx * line_height), line, fill=color, font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def render_caption_image(path: Path, text: str) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (1600, 220), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle((14, 14, 1586, 206), radius=30, fill=(0, 0, 0, 178))

    font = load_font(size=52, bold=True)
    wrapped = wrap_text(draw, text, font=font, max_width=1450)[:2]
    line_height = 62
    total_h = line_height * len(wrapped)
    y0 = int((220 - total_h) / 2)
    for i, line in enumerate(wrapped):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1600 - w) / 2, y0 + i * line_height), line, fill=(255, 255, 255, 255), font=font)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def src_to_dst_time(t: float, timeline: Sequence[Dict]) -> Optional[float]:
    for m in timeline:
        if m["src_start"] <= t <= m["src_end"]:
            return m["dst_start"] + (t - m["src_start"])
    return None


def pick_captions(cuts: Dict, timeline: Sequence[Dict]) -> List[Dict]:
    chunks = cuts.get("scored_chunks", [])
    selected: List[Dict] = []

    def add_caption(start_src: float, end_src: float, text: str) -> None:
        dst_s = src_to_dst_time(start_src, timeline)
        dst_e = src_to_dst_time(min(end_src, start_src + 6.0), timeline)
        if dst_s is None or dst_e is None or dst_e - dst_s < 1.2:
            return
        selected.append(
            {
                "start": round(dst_s + 0.08, 2),
                "end": round(min(dst_e, dst_s + 3.8), 2),
                "text": " ".join(text.split())[:72],
            }
        )

    hook = next((c for c in chunks if c["start"] < 40.0 and len(c["text"]) > 35), None)
    if hook:
        add_caption(hook["start"], hook["end"], hook["text"])

    sorted_emphasis = sorted(
        (
            c
            for c in chunks
            if c["score"] > 1.5
            and 2.0 <= (c["end"] - c["start"]) <= 9.0
            and len(c["text"]) > 25
            and c["start"] > 35.0
        ),
        key=lambda c: c["score"],
        reverse=True,
    )

    last_start = -999.0
    for c in sorted_emphasis:
        if len(selected) >= 4:
            break
        dst_s = src_to_dst_time(c["start"], timeline)
        if dst_s is None:
            continue
        if dst_s - last_start < 45.0:
            continue
        add_caption(c["start"], c["end"], c["text"])
        last_start = dst_s

    selected = sorted(selected, key=lambda x: x["start"])
    return selected[:4]


def build_caption_overlay_graph(captions: Sequence[Dict]) -> Tuple[str, str]:
    parts = ["[0:v]scale=1920:1080:flags=lanczos[v0]"]
    for idx, c in enumerate(captions, start=1):
        parts.append(
            f"[v{idx-1}][{idx}:v]"
            f"overlay=x=(W-w)/2:y=H-h-120:enable='between(t,{c['start']:.2f},{c['end']:.2f})'"
            f"[v{idx}]"
        )
    out_label = f"[v{len(captions)}]"
    return ";".join(parts), out_label


def make_intro(paths: Paths, title: str, hook: str) -> None:
    intro_img = paths.cards_dir / "intro_card.png"
    render_card_image(
        intro_img,
        bg_rgb=(14, 26, 43),
        lines=[
            {"text": title, "size": 84, "y": 390, "color": (255, 255, 255), "bold": True, "max_width": 1700},
            {"text": hook, "size": 46, "y": 560, "color": (213, 232, 255), "bold": False, "max_width": 1600},
        ],
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            "30",
            "-loop",
            "1",
            "-i",
            str(intro_img),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "6",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(paths.intro_video),
        ]
    )


def make_outro(paths: Paths) -> None:
    outro_img = paths.cards_dir / "outro_card.png"
    render_card_image(
        outro_img,
        bg_rgb=(16, 16, 24),
        lines=[
            {"text": "Thanks for watching", "size": 80, "y": 330, "color": (255, 255, 255), "bold": True},
            {
                "text": "Subscribe for more AI workflows",
                "size": 50,
                "y": 510,
                "color": (213, 232, 255),
                "bold": False,
            },
            {
                "text": "Watch the next video on this channel",
                "size": 44,
                "y": 620,
                "color": (200, 250, 204),
                "bold": False,
            },
        ],
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            "30",
            "-loop",
            "1",
            "-i",
            str(outro_img),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "10",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(paths.outro_video),
        ]
    )


def polish_final(paths: Paths, title: str, hook: str) -> None:
    cuts = load_json(paths.cuts_json)
    timeline = load_json(paths.timeline_map_json)["timeline"]
    captions = pick_captions(cuts, timeline)

    caption_images: List[Path] = []
    for i, cap in enumerate(captions, start=1):
        cp = paths.cards_dir / f"caption_{i:02d}.png"
        render_caption_image(cp, cap["text"])
        caption_images.append(cp)
        cap["image"] = str(cp)
    save_json(paths.caption_json, {"captions": captions})

    graph, out_label = build_caption_overlay_graph(captions)
    af = (
        "highpass=f=80,"
        "lowpass=f=14000,"
        "acompressor=threshold=-18dB:ratio=3:attack=20:release=250:makeup=2,"
        "alimiter=limit=0.95,"
        "loudnorm=I=-14:TP=-1.5:LRA=11"
    )

    body_cmd = ["ffmpeg", "-y", "-i", str(paths.rough_video)]
    for cp in caption_images:
        body_cmd.extend(["-loop", "1", "-i", str(cp)])
    body_cmd.extend(
        [
            "-filter_complex",
            graph,
            "-map",
            out_label,
            "-map",
            "0:a",
            "-af",
            af,
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(paths.polished_body),
        ]
    )
    run(body_cmd)

    make_intro(paths, title=title, hook=hook)
    make_outro(paths)

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(paths.intro_video),
            "-i",
            str(paths.polished_body),
            "-i",
            str(paths.outro_video),
            "-filter_complex",
            "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(paths.final_video),
        ]
    )
    print(f"Wrote final export: {paths.final_video}")
    print(f"Wrote captions plan: {paths.caption_json}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transcript-driven video edit pipeline")
    p.add_argument(
        "--stage",
        choices=["all", "transcribe", "detect-cuts", "assemble-rough", "polish-final", "export"],
        default="all",
    )
    p.add_argument("--root", default=str(Path.cwd()))
    p.add_argument("--source", default="input/source.mp4")
    p.add_argument("--model-size", default="small.en")
    p.add_argument("--language", default="en")
    p.add_argument("--target-min-minutes", type=float, default=8.0)
    p.add_argument("--target-goal-minutes", type=float, default=10.0)
    p.add_argument("--target-max-minutes", type=float, default=12.0)
    p.add_argument(
        "--title",
        default="Building a High Quality Content Factory with AI Tools",
    )
    p.add_argument(
        "--hook",
        default="A practical workflow to create better content faster",
    )
    return p.parse_args()


def build_paths(root: Path, source_rel: str) -> Paths:
    cache_override = os.getenv("CFS_TRANSCRIPT_CACHE_DIR", "").strip()
    if cache_override:
        transcript_cache_dir = Path(cache_override).expanduser().resolve()
    else:
        transcript_cache_dir = Path(__file__).resolve().parents[1] / "work" / "cache" / "transcripts"

    return Paths(
        root=root,
        source=(root / source_rel).resolve(),
        audio=root / "work" / "audio" / "source_16k.wav",
        transcript_json=root / "work" / "transcript" / "transcript.json",
        cuts_json=root / "work" / "cuts" / "cut_decisions.json",
        cuts_csv=root / "work" / "cuts" / "cut_decisions_log.csv",
        keep_json=root / "work" / "timelines" / "keep_intervals.json",
        rough_filter=root / "work" / "timelines" / "rough_filter.ffscript",
        rough_video=root / "work" / "renders" / "v1_rough_internal.mp4",
        intro_video=root / "work" / "renders" / "intro.mp4",
        outro_video=root / "work" / "renders" / "outro.mp4",
        polished_body=root / "work" / "renders" / "v2_polished_body.mp4",
        review_video=root / "exports" / "v1_rough_cut.mp4",
        final_video=root / "exports" / "v2_final_youtube_1080p.mp4",
        caption_json=root / "work" / "cuts" / "selected_captions.json",
        timeline_map_json=root / "work" / "timelines" / "timeline_map.json",
        cards_dir=root / "work" / "cards",
        transcript_cache_dir=transcript_cache_dir,
    )


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    paths = build_paths(root, args.source)
    ensure_dirs(paths)

    if not paths.source.exists():
        raise FileNotFoundError(f"Source video not found: {paths.source}")

    stage = args.stage
    target_min = args.target_min_minutes * 60.0
    target_goal = args.target_goal_minutes * 60.0
    target_max = args.target_max_minutes * 60.0

    if stage in ("all", "transcribe"):
        transcribe(paths, model_size=args.model_size, language=args.language)
    if stage in ("all", "detect-cuts"):
        detect_cuts(paths, target_min=target_min, target_goal=target_goal, target_max=target_max)
    if stage in ("all", "assemble-rough"):
        assemble_rough(paths)
    if stage in ("all", "polish-final", "export"):
        polish_final(paths, title=args.title, hook=args.hook)


if __name__ == "__main__":
    main()
