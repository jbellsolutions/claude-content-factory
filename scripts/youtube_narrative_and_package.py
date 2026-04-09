#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


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
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "with",
    "you",
    "we",
    "i",
    "this",
    "so",
}

KEYWORDS = {
    "ai",
    "content",
    "factory",
    "workflow",
    "system",
    "app",
    "youtube",
    "clips",
    "shorts",
    "automation",
    "prompt",
    "deploy",
    "video",
}


def run(cmd: Sequence[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text())


def save_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


def normalize_word(raw: str) -> str:
    chars = []
    for ch in raw.lower():
        if ch.isalnum():
            chars.append(ch)
    return "".join(chars)


def merge_intervals(intervals: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    items = sorted((s, e) for s, e in intervals if e > s)
    out = [items[0]]
    for s, e in items[1:]:
        ls, le = out[-1]
        if s <= le + 0.02:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def narrative_keep_intervals(cuts: Dict, words: Sequence[Dict]) -> List[Tuple[float, float]]:
    keep = [(float(x["start"]), float(x["end"])) for x in cuts["keep_intervals"]]
    restore: List[Tuple[float, float]] = []
    connectors = {"and", "so", "but", "then", "if", "let", "now", "because"}
    for i in range(len(keep) - 1):
        _, b = keep[i]
        c, _ = keep[i + 1]
        gap = c - b
        if gap <= 0:
            continue
        gap_words = [
            normalize_word(w["word"])
            for w in words
            if float(w["start"]) >= b and float(w["end"]) <= c
        ]
        gap_words = [w for w in gap_words if w]
        if gap <= 2.2:
            restore.append((b, c))
            continue
        if gap <= 3.0 and len(gap_words) <= 9 and any(w in connectors for w in gap_words):
            restore.append((b, c))

    rebuilt: List[Tuple[float, float]] = []
    cur_s, cur_e = keep[0]
    restore_set = {(round(a, 3), round(b, 3)) for a, b in restore}
    for i in range(len(keep) - 1):
        _, b = keep[i]
        c, d = keep[i + 1]
        key = (round(b, 3), round(c, 3))
        if key in restore_set:
            cur_e = d
        else:
            rebuilt.append((cur_s, cur_e))
            cur_s, cur_e = c, d
    rebuilt.append((cur_s, cur_e))
    return merge_intervals(rebuilt)


def write_concat_filter(path: Path, keep: Sequence[Tuple[float, float]]) -> None:
    lines: List[str] = []
    for i, (s, e) in enumerate(keep):
        lines.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];")
        lines.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}];")
    inter = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
    lines.append(f"{inter}concat=n={len(keep)}:v=1:a=1[v][a]")
    path.write_text("\n".join(lines) + "\n")


def build_timeline_map(keep: Sequence[Tuple[float, float]]) -> List[Dict]:
    out: List[Dict] = []
    cursor = 0.0
    for s, e in keep:
        d = e - s
        out.append(
            {
                "src_start": round(s, 3),
                "src_end": round(e, 3),
                "dst_start": round(cursor, 3),
                "dst_end": round(cursor + d, 3),
            }
        )
        cursor += d
    return out


def src_to_dst(t: float, timeline: Sequence[Dict]) -> Optional[float]:
    for x in timeline:
        if x["src_start"] <= t <= x["src_end"]:
            return x["dst_start"] + (t - x["src_start"])
    return None


def make_caption_image(path: Path, text: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (1600, 220), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle((14, 14, 1586, 206), radius=28, fill=(0, 0, 0, 178))

    font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    if Path(font_path).exists():
        font = ImageFont.truetype(font_path, size=52)
    else:
        font = ImageFont.load_default()

    words = text.split()
    lines: List[str] = []
    cur = words[0] if words else ""
    for w in words[1:]:
        trial = f"{cur} {w}".strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= 1450:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:2]
    y = 66 if len(lines) == 1 else 36
    for ln in lines:
        bbox = draw.textbbox((0, 0), ln, font=font)
        x = (1600 - (bbox[2] - bbox[0])) / 2
        draw.text((x, y), ln, fill=(255, 255, 255, 255), font=font)
        y += 62
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def pick_captions(transcript: Dict, timeline: Sequence[Dict]) -> List[Dict]:
    segments = transcript["segments"]
    candidates: List[Dict] = []
    for seg in segments:
        dst = src_to_dst(float(seg["start"]), timeline)
        if dst is None:
            continue
        text = " ".join(seg["text"].split())
        if len(text) < 35:
            continue
        toks = [normalize_word(t) for t in text.split()]
        score = sum(1 for t in toks if t in KEYWORDS) + min(4, len(toks) // 8)
        candidates.append(
            {
                "src_start": float(seg["start"]),
                "src_end": float(seg["end"]),
                "dst_start": float(dst),
                "text": text[:78],
                "score": score,
            }
        )
    candidates.sort(key=lambda x: (x["score"], -x["dst_start"]), reverse=True)
    chosen: List[Dict] = []
    for c in candidates:
        if c["dst_start"] < 8:
            continue
        if any(abs(c["dst_start"] - x["start"]) < 70 for x in chosen):
            continue
        end_dst = src_to_dst(min(c["src_end"], c["src_start"] + 4.5), timeline)
        if end_dst is None or end_dst - c["dst_start"] < 1.2:
            continue
        chosen.append(
            {
                "start": round(c["dst_start"] + 0.08, 2),
                "end": round(min(end_dst, c["dst_start"] + 3.9), 2),
                "text": c["text"],
            }
        )
        if len(chosen) >= 3:
            break
    chosen.sort(key=lambda x: x["start"])
    return chosen


def chapter_title_from_text(text: str) -> str:
    low = text.lower()
    if "content factory" in low:
        return "What The Content Factory Actually Does"
    if "workflow" in low or "system" in low:
        return "Inside The Workflow System"
    if "transcript" in low or "timestamps" in low:
        return "Transcript And Timestamp Layer"
    if "clips" in low or "shorts" in low or "reels" in low:
        return "Turning Long Video Into Shorts"
    if "youtube" in low or "thumbnail" in low or "description" in low:
        return "YouTube Packaging Strategy"
    if "deploy" in low or "live url" in low or "app" in low:
        return "Deploying And Testing The App"
    if "call" in low or "book" in low or "link below" in low:
        return "CTA And Conversion Flow"
    if "support" in low or "community" in low:
        return "Final Takeaways And Next Steps"
    words = [normalize_word(w) for w in text.split()]
    words = [w for w in words if w and w not in STOPWORDS and len(w) > 2]
    words = words[:6] if words else ["Key", "Section"]
    return " ".join(w.capitalize() for w in words)


def format_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def build_chapters(mapped_segments: Sequence[Dict], total_duration: float) -> List[Dict]:
    n = max(7, min(10, round(total_duration / 75)))
    starts = [0.0]
    targets = [i * (total_duration / n) for i in range(1, n)]
    seg_starts = [s["dst_start"] for s in mapped_segments if s["dst_start"] > 12]
    for t in targets:
        choices = [x for x in seg_starts if abs(x - t) <= 24 and x - starts[-1] >= 45]
        if choices:
            starts.append(min(choices, key=lambda x: abs(x - t)))
        else:
            fallback = [x for x in seg_starts if x - starts[-1] >= 55]
            if fallback:
                starts.append(fallback[0])
    starts = sorted(set(round(x, 2) for x in starts))

    chapters: List[Dict] = []
    for st in starts:
        nearby = [s for s in mapped_segments if s["dst_start"] >= st][:5]
        sample = " ".join(s["text"] for s in nearby)[:240] if nearby else "Section"
        chapters.append({"start": st, "title": chapter_title_from_text(sample)})
    chapters[0]["title"] = "Intro And What You Will Learn"
    return chapters


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=str(Path.cwd()))
    p.add_argument("--source", default="input/source.mp4")
    args = p.parse_args()
    root = Path(args.root).resolve()

    source = (root / args.source).resolve()
    transcript_path = root / "work" / "transcript" / "transcript.json"
    cuts_path = root / "work" / "cuts" / "cut_decisions.json"

    v3_keep_path = root / "work" / "timelines" / "v3_keep_intervals.json"
    v3_map_path = root / "work" / "timelines" / "v3_timeline_map.json"
    v3_filter_path = root / "work" / "timelines" / "v3_filter.ffscript"
    v3_raw = root / "work" / "renders" / "v3_narrative_raw.mp4"
    v3_polished = root / "work" / "renders" / "v3_narrative_polished.mp4"
    v3_final = root / "exports" / "v3_narrative_flow_youtube.mp4"

    intro = root / "work" / "renders" / "intro.mp4"
    outro = root / "work" / "renders" / "outro.mp4"
    cards_dir = root / "work" / "cards"
    caption_plan_path = root / "work" / "cuts" / "v3_selected_captions.json"
    chapters_path = root / "exports" / "v3_chapters.txt"
    description_path = root / "exports" / "v3_youtube_description.md"

    transcript = load_json(transcript_path)
    cuts = load_json(cuts_path)
    words = transcript["words"]

    keep = narrative_keep_intervals(cuts, words)
    timeline = build_timeline_map(keep)
    save_json(v3_keep_path, {"keep_intervals": [{"start": s, "end": e} for s, e in keep]})
    save_json(v3_map_path, {"timeline": timeline})
    write_concat_filter(v3_filter_path, keep)

    if not v3_raw.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-filter_complex_script",
                str(v3_filter_path),
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
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(v3_raw),
            ]
        )
    else:
        print(f"Reusing existing raw render: {v3_raw}")

    captions = pick_captions(transcript, timeline)
    caption_images: List[Path] = []
    for i, c in enumerate(captions, start=1):
        cp = cards_dir / f"v3_caption_{i:02d}.png"
        make_caption_image(cp, c["text"])
        c["image"] = str(cp)
        caption_images.append(cp)
    save_json(caption_plan_path, {"captions": captions})

    parts = ["[0:v]scale=1920:1080:flags=lanczos[v0]"]
    for i, c in enumerate(captions, start=1):
        parts.append(
            f"[v{i-1}][{i}:v]"
            f"overlay=x=(W-w)/2:y=H-h-120:enable='between(t,{c['start']:.2f},{c['end']:.2f})'"
            f"[v{i}]"
        )
    graph = ";".join(parts)
    out_label = f"[v{len(captions)}]"

    cmd = ["ffmpeg", "-y", "-i", str(v3_raw)]
    for cp in caption_images:
        cmd.extend(["-loop", "1", "-i", str(cp)])
    cmd.extend(
        [
            "-filter_complex",
            graph,
            "-map",
            out_label,
            "-map",
            "0:a",
            "-af",
            "highpass=f=80,lowpass=f=14000,acompressor=threshold=-18dB:ratio=3:attack=20:release=250:makeup=2,alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11",
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
            str(v3_polished),
        ]
    )
    run(cmd)

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(intro),
            "-i",
            str(v3_polished),
            "-i",
            str(outro),
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
            str(v3_final),
        ]
    )

    mapped_segments: List[Dict] = []
    for seg in transcript["segments"]:
        ds = src_to_dst(float(seg["start"]), timeline)
        if ds is None:
            continue
        mapped_segments.append({"dst_start": ds, "text": " ".join(seg["text"].split())})
    mapped_segments.sort(key=lambda x: x["dst_start"])

    total = ffprobe_duration(v3_final)
    chapters = build_chapters(mapped_segments, total)
    chapter_lines = [f"{format_mmss(c['start'])} - {c['title']}" for c in chapters]
    chapters_path.write_text("\n".join(chapter_lines) + "\n")

    description = []
    description.append("I break down how I build a high-quality AI content factory from a single long-form video.")
    description.append("")
    description.append("In this video:")
    description.append("- How the content factory workflow is structured")
    description.append("- How one recording turns into shorts and distribution-ready assets")
    description.append("- How to package the output for YouTube and deployment")
    description.append("")
    description.append("Chapters:")
    for line in chapter_lines:
        description.append(f"- {line}")
    description.append("")
    description.append("If you want this workflow for your own content system, subscribe and watch the next video.")
    description.append("")
    description.append("#AI #ContentCreation #YouTubeAutomation #Workflow")
    description_path.write_text("\n".join(description) + "\n")

    keep_duration = sum(e - s for s, e in keep)
    print(f"V3 keep intervals: {len(keep)}")
    print(f"V3 body duration: {keep_duration/60:.2f} min")
    print(f"Wrote: {v3_final}")
    print(f"Wrote: {chapters_path}")
    print(f"Wrote: {description_path}")


if __name__ == "__main__":
    main()
