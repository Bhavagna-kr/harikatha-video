"""
Harikatha video prototype — turns a saved performance + its narration WAV into an MP4.

This script is standalone: it does NOT touch the app's backend or frontend.

Pipeline (one image per section):
  1. Load the performance JSON (sections with title + content).
  2. Work out how long each section lasts inside the narration WAV.
  3. Make one picture per section (placeholder card, or a local AI image).
  4. Animate each picture with a slow zoom/pan ("Ken Burns" effect) for its duration.
  5. Join the clips, add the narration audio, and burn in subtitles.

Usage:
  python make_video.py performance.json --audio narration.wav --out harikatha.mp4
  python make_video.py performance.json --audio narration.wav --images sd      # AI images (Mac M-series)
  python make_video.py performance.json --audio narration.wav --images sd --ollama  # + AI-written image prompts
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT, FPS = 1280, 720, 25


# ---------------------------------------------------------------- 1. load input
def load_sections(path: Path) -> tuple[str, list[dict]]:
    """Accept either the app's 'Download performance JSON' file or a full saved record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    perf = data.get("performance", data)  # full record has a "performance" key; the download is the performance itself
    sections = [s for s in perf.get("sections", []) if str(s.get("content", "")).strip()]
    if not sections:
        sys.exit("No sections with content found in the JSON.")
    return perf.get("title", "Harikatha"), sections


def section_text(sec: dict) -> str:
    # Same text the backend narrates: "title.\ncontent" (see generate_full_narration in main.py)
    return f"{sec.get('title', '')}.\n{sec.get('content', '')}".strip()


# ---------------------------------------------------------------- 2. timing
def audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def section_durations(sections: list[dict], total: float | None) -> list[float]:
    """The full narration is one WAV, so we split its length in proportion to each section's
    text length. Piper reads at a steady pace, so this lines up closely enough for slides."""
    lengths = [len(section_text(s)) for s in sections]
    if total is None:  # no audio: assume ~15 characters per second of speech
        return [max(3.0, n / 15) for n in lengths]
    return [total * n / sum(lengths) for n in lengths]


# ---------------------------------------------------------------- 3a. prompts
SCENE_STYLE = {
    "battle": "epic battlefield, chariots, banners, dust and golden light",
    "storm": "dramatic storm clouds, lightning, rain over ancient land",
    "river": "serene sacred river at dawn, ghats, soft mist",
    "forest": "lush ancient forest hermitage, sunbeams through trees",
    "temple": "ancient South Indian temple, oil lamps, warm glow",
    "divine": "divine radiant figure with flute, Vrindavan, peacocks, soft glow",
    "emotional": "solitary figure in quiet sorrow, dusk, muted colours",
    "devotional": "peaceful devotional scene, oil lamps, marigolds, warm light",
}
STYLE_SUFFIX = "traditional Indian painting style, Tanjore and Mysore art inspired, rich colours, highly detailed, no text"


def detect_scene(text: str) -> str:
    """Import the app's own scene detector if available, so video and audio agree."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
        from app.main import detect_scene as app_detect  # type: ignore
        return app_detect(text)
    except Exception:
        return "devotional"


def template_prompt(sec: dict) -> str:
    scene = detect_scene(section_text(sec))
    return f"{sec.get('title', '')}, {SCENE_STYLE.get(scene, SCENE_STYLE['devotional'])}, {STYLE_SUFFIX}"


def ollama_prompt(sec: dict, model: str, base_url: str) -> str:
    """Ask the local LLM to describe one picture for this section. Falls back to the template."""
    try:
        import httpx
        instruction = (
            "Describe ONE illustration for this part of a story in under 40 words: who is in it, "
            "where, what is happening, lighting. Only visual details, no text or letters. "
            "Reply with the description only.\n\n" + section_text(sec)[:1500]
        )
        r = httpx.post(f"{base_url.rstrip('/')}/api/generate",
                       json={"model": model, "prompt": instruction, "stream": False}, timeout=120)
        r.raise_for_status()
        desc = r.json().get("response", "").strip().replace("\n", " ")
        if desc:
            return f"{desc}, {STYLE_SUFFIX}"
    except Exception as exc:
        print(f"  (Ollama prompt failed, using template: {exc})")
    return template_prompt(sec)


# ---------------------------------------------------------------- 3b. images
def find_font(size: int) -> ImageFont.ImageFont:
    for p in ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf",       # macOS
              "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",     # Linux
              "C:/Windows/Fonts/georgiab.ttf"]:                            # Windows
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


PALETTES = [((88, 28, 20), (212, 140, 40)), ((20, 40, 80), (200, 150, 70)),
            ((40, 60, 30), (210, 170, 80)), ((70, 20, 60), (230, 130, 60))]


def placeholder_image(sec: dict, index: int, path: Path) -> None:
    """No-AI fallback: a warm gradient card with the section type and title."""
    top, bottom = PALETTES[index % len(PALETTES)]
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # vertical gradient, one line at a time
        t = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    img = img.filter(ImageFilter.GaussianBlur(2))
    draw = ImageDraw.Draw(img)
    kicker = str(sec.get("type", "section")).upper()
    title = str(sec.get("title", ""))[:60]
    for text, font, y in [(kicker, find_font(28), HEIGHT * 0.36), (title, find_font(56), HEIGHT * 0.45)]:
        w = draw.textlength(text, font=font)
        draw.text(((WIDTH - w) / 2, y), text, font=font, fill=(255, 240, 210))
    img.save(path)


class SDImages:
    """Local AI images with Hugging Face diffusers. Uses the Mac GPU ('mps') when present.
    sd-turbo makes a usable image in 1-4 steps, so it is fast enough for a demo."""

    def __init__(self, model_id: str):
        import torch
        from diffusers import AutoPipelineForText2Image
        device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device != "cpu" else torch.float32
        print(f"Loading {model_id} on {device} (first run downloads the model)…")
        self.pipe = AutoPipelineForText2Image.from_pretrained(model_id, torch_dtype=dtype).to(device)

    def make(self, prompt: str, path: Path) -> None:
        # 768x432 keeps 16:9; FFmpeg upscales it to 1280x720 later.
        image = self.pipe(prompt=prompt, num_inference_steps=4, guidance_scale=0.0, width=768, height=432).images[0]
        image.save(path)


# ---------------------------------------------------------------- 4. animate
def ken_burns_clip(image: Path, seconds: float, index: int, out: Path) -> None:
    frames = max(1, round(seconds * FPS))
    # Alternate zoom-in and zoom-out so consecutive slides don't feel identical.
    zoom = f"min(1+0.12*on/{frames},1.12)" if index % 2 == 0 else f"max(1.12-0.12*on/{frames},1.0)"
    vf = (
        f"scale={WIDTH * 4}:{HEIGHT * 4}:force_original_aspect_ratio=increase,crop={WIDTH * 4}:{HEIGHT * 4},"  # big canvas = smooth zoom
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0, seconds - 0.5):.2f}:d=0.5,format=yuv420p"
    )
    run(["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-vf", vf, "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out)])


# ---------------------------------------------------------------- 5. subtitles + final mux
def fmt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def chunk_text(text: str, max_chars: int = 90) -> list[str]:
    """Split into sentence-sized caption lines, breaking long sentences at spaces."""
    pieces = []
    for sentence in re.split(r"(?<=[.!?।])\s+", text.replace("\n", " ")):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars) if " " in sentence[:max_chars] else max_chars
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence.strip():
            pieces.append(sentence.strip())
    return pieces


def write_srt(sections: list[dict], durations: list[float], path: Path) -> None:
    lines, n, start = [], 1, 0.0
    for sec, dur in zip(sections, durations):
        chunks = chunk_text(section_text(sec))
        total_chars = sum(len(c) for c in chunks) or 1
        t = start
        for c in chunks:  # time each caption by its share of the section's characters
            d = dur * len(c) / total_chars
            lines.append(f"{n}\n{fmt_time(t)} --> {fmt_time(t + d)}\n{c}\n")
            n, t = n + 1, t + d
        start += dur
    path.write_text("\n".join(lines), encoding="utf-8")


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd[:4])}…\n{proc.stderr[-1500:]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("performance", type=Path, help="performance JSON (downloaded from the app, or a saved record)")
    ap.add_argument("--audio", type=Path, help="full narration WAV from 'Generate Full Narration'")
    ap.add_argument("--out", type=Path, default=Path("harikatha.mp4"))
    ap.add_argument("--images", choices=["placeholder", "sd"], default="placeholder")
    ap.add_argument("--sd-model", default="stabilityai/sd-turbo")
    ap.add_argument("--ollama", action="store_true", help="let the local LLM write the image prompts")
    ap.add_argument("--ollama-model", default="qwen2.5:3b")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--no-subtitles", action="store_true")
    args = ap.parse_args()
    # The final ffmpeg step runs inside a temp folder, so make every path absolute first.
    args.performance = args.performance.resolve()
    args.audio = args.audio.resolve() if args.audio else None

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH (Mac: brew install ffmpeg).")

    title, sections = load_sections(args.performance)
    total = audio_duration(args.audio) if args.audio else None
    durations = section_durations(sections, total)
    print(f"'{title}': {len(sections)} sections, {sum(durations):.1f}s total")

    work = Path(tempfile.mkdtemp(prefix="harikatha_video_"))
    sd = SDImages(args.sd_model) if args.images == "sd" else None

    clips = []
    for i, (sec, dur) in enumerate(zip(sections, durations)):
        img = work / f"img_{i}.png"
        if sd:
            prompt = ollama_prompt(sec, args.ollama_model, args.ollama_url) if args.ollama else template_prompt(sec)
            print(f"[{i + 1}/{len(sections)}] {prompt[:100]}…")
            sd.make(prompt, img)
        else:
            print(f"[{i + 1}/{len(sections)}] placeholder card: {sec.get('title', '')}")
            placeholder_image(sec, i, img)
        clip = work / f"clip_{i}.mp4"
        ken_burns_clip(img, dur, i, clip)
        clips.append(clip)

    # Join all silent clips into one video.
    concat_list = work / "clips.txt"
    concat_list.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(joined)])

    # Final pass: add narration + burn subtitles.
    cmd = ["ffmpeg", "-y", "-i", str(joined)]
    if args.audio:
        cmd += ["-i", str(args.audio)]
    if not args.no_subtitles:
        srt = work / "subs.srt"
        write_srt(sections, durations, srt)
        style = "FontSize=18,PrimaryColour=&H00F0FFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,MarginV=30"
        # Run from the work dir so the subtitles filter gets a plain filename (avoids path-escaping issues).
        cmd += ["-vf", f"subtitles=subs.srt:force_style='{style}'"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if args.audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    out = args.out.resolve()  # resolved before cwd changes
    proc = subprocess.run(cmd + [str(out)], cwd=work, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"Final render failed:\n{proc.stderr[-1500:]}")

    shutil.rmtree(work, ignore_errors=True)
    print(f"Done → {out}")


if __name__ == "__main__":
    main()
