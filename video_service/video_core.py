"""
video_core — the video engine. No web server, no app imports: just "story JSON in, MP4 out".

Pipeline (one image per section):
  1. Read the sections (title + content) from the performance JSON.
  2. Work out how long each section lasts (from the narration audio, or from text length).
  3. Make one picture per section (placeholder card, or a local AI image).
  4. Draw subtitles as transparent pictures.
  5. Animate each picture (slow zoom), lay the subtitles on top, join the clips, add the audio.

Everything the rest of the system needs is `render_video(...)`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT, FPS = 1280, 720, 25
Progress = Callable[[float, str], None]  # progress(fraction 0..1, message)


class VideoError(Exception):
    """Raised for any problem the user should see (bad input, ffmpeg failure, ...)."""


# ---------------------------------------------------------------- 1. input
def get_sections(data: dict) -> tuple[str, list[dict]]:
    """Accept the performance itself ({title, sections}) or a full saved record ({performance: {...}}).
    This is the ONLY place that knows the input shape — if the team renames fields, change it here."""
    perf = data.get("performance", data)
    sections = [s for s in perf.get("sections", []) if isinstance(s, dict) and str(s.get("content", "")).strip()]
    if not sections:
        raise VideoError("No sections with content found in the performance JSON.")
    return str(perf.get("title") or "Harikatha"), sections


def section_text(sec: dict) -> str:
    # Same text the backend narrates: "title.\ncontent"
    return f"{sec.get('title', '')}.\n{sec.get('content', '')}".strip()


# ---------------------------------------------------------------- 2. timing
def audio_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise VideoError("Could not read the narration audio file.") from exc


def section_durations(sections: list[dict], total: float | None) -> list[float]:
    """Narration is one long file, so split its length in proportion to each section's text length.
    Piper reads at a steady pace, so this lines up closely enough. No audio: ~15 characters/second."""
    lengths = [len(section_text(s)) for s in sections]
    if total is None:
        return [max(3.0, n / 15) for n in lengths]
    return [total * n / sum(lengths) for n in lengths]


# ---------------------------------------------------------------- 3a. image prompts
# Own copy of the scene keywords (instead of importing the backend) so this service never breaks
# when the backend changes.
SCENES = {
    "battle": (["battle", "war", "kurukshetra", "army", "warrior", "bow", "arrows", "chariot", "fight", "weapon"],
               "epic battlefield, chariots, banners, dust and golden light"),
    "storm": (["storm", "thunder", "lightning", "heavy rain", "tempest", "flood"],
              "dramatic storm clouds, lightning, rain over ancient land"),
    "river": (["river", "yamuna", "ganga", "boat", "waterfall"], "serene sacred river at dawn, ghats, soft mist"),
    "forest": (["forest", "jungle", "trees", "hermitage", "ashram", "wilderness"],
               "lush ancient forest hermitage, sunbeams through trees"),
    "temple": (["temple", "prayer", "worship", "aarti", "puja", "devotees"], "ancient South Indian temple, oil lamps, warm glow"),
    "divine": (["krishna", "vrindavan", "flute", "radha", "gopis", "vishnu", "shiva"],
               "divine radiant figure, Vrindavan, peacocks, soft glow"),
    "emotional": (["wept", "tears", "grief", "sorrow", "separation", "heartbroken"],
                  "solitary figure in quiet sorrow, dusk, muted colours"),
}
DEFAULT_SCENE = "peaceful devotional scene, oil lamps, marigolds, warm light"
STYLE = "traditional Indian painting style, Tanjore and Mysore art inspired, rich colours, highly detailed, no text"


def template_prompt(sec: dict) -> str:
    text = section_text(sec).lower()
    scene = next((desc for words, desc in SCENES.values() if any(w in text for w in words)), DEFAULT_SCENE)
    return f"{sec.get('title', '')}, {scene}, {STYLE}"


def llm_prompt(sec: dict, ollama_url: str, model: str) -> str:
    """Ask the local LLM (Ollama, free) to describe one picture for this section. Falls back to the template."""
    try:
        import httpx
        instruction = ("Describe ONE illustration for this part of a story in under 40 words: who is in it, where, "
                       "what is happening, lighting. Only visual details, no text or letters. Reply with the "
                       "description only.\n\n" + section_text(sec)[:1500])
        r = httpx.post(f"{ollama_url.rstrip('/')}/api/generate",
                       json={"model": model, "prompt": instruction, "stream": False}, timeout=120)
        r.raise_for_status()
        desc = r.json().get("response", "").strip().replace("\n", " ")
        if desc:
            return f"{desc}, {STYLE}"
    except Exception:
        pass
    return template_prompt(sec)


# ---------------------------------------------------------------- 3b. images
def _font(paths: list[str], size: int) -> ImageFont.ImageFont:
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


TITLE_FONTS = ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", "C:/Windows/Fonts/georgiab.ttf"]
# Arial Unicode (Mac) also covers Hindi/Telugu/Tamil/Kannada letters.
CAPTION_FONTS = ["/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]
PALETTES = [((88, 28, 20), (212, 140, 40)), ((20, 40, 80), (200, 150, 70)),
            ((40, 60, 30), (210, 170, 80)), ((70, 20, 60), (230, 130, 60))]


def placeholder_image(sec: dict, index: int, path: Path) -> None:
    """No-AI fallback: a warm gradient card with the section type and title."""
    top, bottom = PALETTES[index % len(PALETTES)]
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    img = img.filter(ImageFilter.GaussianBlur(2))
    draw = ImageDraw.Draw(img)
    for text, size, y in [(str(sec.get("type", "")).upper(), 28, 0.36), (str(sec.get("title", ""))[:60], 56, 0.45)]:
        font = _font(TITLE_FONTS, size)
        draw.text(((WIDTH - draw.textlength(text, font=font)) / 2, HEIGHT * y), text, font=font, fill=(255, 240, 210))
    img.save(path)


_SD_PIPE = None  # loaded once, reused for every video (loading takes a while)


def ai_available() -> bool:
    try:
        import diffusers  # noqa: F401
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def ai_image(prompt: str, path: Path, model_id: str) -> None:
    """Local AI image with diffusers. Uses the Mac GPU ('mps') when present; sd-turbo needs only 1-4 steps."""
    global _SD_PIPE
    if not ai_available():
        raise VideoError("AI images need: pip install -r requirements-ai.txt")
    import torch
    from diffusers import AutoPipelineForText2Image
    if _SD_PIPE is None:
        device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device != "cpu" else torch.float32
        _SD_PIPE = AutoPipelineForText2Image.from_pretrained(model_id, torch_dtype=dtype).to(device)
    image = _SD_PIPE(prompt=prompt, num_inference_steps=4, guidance_scale=0.0, width=768, height=432).images[0]
    image.save(path)


# ---------------------------------------------------------------- 4. subtitles
# Drawn with Pillow instead of FFmpeg's "subtitles" filter, because Homebrew FFmpeg has no libass.
def chunk_text(text: str, max_chars: int = 90) -> list[str]:
    pieces = []
    for sentence in re.split(r"(?<=[.!?।])\s+", text.replace("\n", " ")):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars) if " " in sentence[:max_chars] else max_chars
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence.strip():
            pieces.append(sentence.strip())
    return pieces


def caption_image(text: str, path: Path) -> None:
    """Transparent 1280x720 PNG: the caption near the bottom, cream text with a dark outline."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(CAPTION_FONTS, 34)
    lines, line = [], ""
    for word in text.split():  # word-wrap to ~85% of the width
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) > WIDTH * 0.85 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    y = HEIGHT - 40 - 44 * len(lines)
    for ln in lines:
        draw.text(((WIDTH - draw.textlength(ln, font=font)) / 2, y), ln, font=font,
                  fill=(255, 248, 230), stroke_width=3, stroke_fill=(20, 10, 5))
        y += 44
    img.save(path)


def section_captions(sec: dict, seconds: float, index: int, work: Path) -> list[tuple[float, float, Path]]:
    chunks = chunk_text(section_text(sec))
    total = sum(len(c) for c in chunks) or 1
    out, t = [], 0.0
    for j, c in enumerate(chunks):  # each caption gets its share of the section's time
        d = seconds * len(c) / total
        png = work / f"cap_{index}_{j}.png"
        caption_image(c, png)
        out.append((t, t + d, png))
        t += d
    return out


# ---------------------------------------------------------------- 5. animate + assemble
def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoError(f"ffmpeg failed: {proc.stderr[-800:]}")


def ken_burns_clip(image: Path, seconds: float, index: int, out: Path,
                   captions: list[tuple[float, float, Path]]) -> None:
    frames = max(1, round(seconds * FPS))
    # Alternate zoom-in / zoom-out so consecutive slides don't feel identical.
    zoom = f"min(1+0.12*on/{frames},1.12)" if index % 2 == 0 else f"max(1.12-0.12*on/{frames},1.0)"
    chain = [f"[0:v]scale={WIDTH * 4}:{HEIGHT * 4}:force_original_aspect_ratio=increase,crop={WIDTH * 4}:{HEIGHT * 4},"
             f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}[v0]"]
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image)]
    last = "v0"
    for k, (a, b, png) in enumerate(captions, start=1):  # show each caption only between its start and end
        cmd += ["-i", str(png)]
        chain.append(f"[{last}][{k}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{k}]")
        last = f"v{k}"
    chain.append(f"[{last}]fade=t=in:st=0:d=0.5,fade=t=out:st={max(0, seconds - 0.5):.2f}:d=0.5,format=yuv420p[out]")
    run(cmd + ["-filter_complex", ";".join(chain), "-map", "[out]", "-frames:v", str(frames),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out)])


def render_video(data: dict, out: Path, audio: Path | None = None, *, images: str = "placeholder",
                 ai_prompts: bool = False, subtitles: bool = True, sd_model: str = "stabilityai/sd-turbo",
                 ollama_url: str = "http://localhost:11434", ollama_model: str = "qwen2.5:3b",
                 progress: Progress = lambda f, m: None) -> Path:
    """The one public function: performance JSON (+ optional narration file) -> MP4 at `out`."""
    if not shutil.which("ffmpeg"):
        raise VideoError("ffmpeg not found on PATH (Mac: brew install ffmpeg).")
    title, sections = get_sections(data)
    durations = section_durations(sections, audio_duration(audio) if audio else None)
    work = Path(tempfile.mkdtemp(prefix="harikatha_video_"))
    try:
        clips = []
        n = len(sections)
        for i, (sec, dur) in enumerate(zip(sections, durations)):
            progress(i / (n + 1), f"Section {i + 1}/{n}: {sec.get('title', '')}")
            img = work / f"img_{i}.png"
            if images == "ai":
                prompt = llm_prompt(sec, ollama_url, ollama_model) if ai_prompts else template_prompt(sec)
                ai_image(prompt, img, sd_model)
            else:
                placeholder_image(sec, i, img)
            caps = section_captions(sec, dur, i, work) if subtitles else []
            clip = work / f"clip_{i}.mp4"
            ken_burns_clip(img, dur, i, clip, caps)
            clips.append(clip)

        progress(n / (n + 1), "Joining clips and adding audio")
        (work / "clips.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
        joined = work / "joined.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "clips.txt"), "-c", "copy", str(joined)])
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-i", str(joined)]
        cmd += (["-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest"] if audio else ["-c", "copy"])
        run(cmd + [str(out)])
        progress(1.0, "Done")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
