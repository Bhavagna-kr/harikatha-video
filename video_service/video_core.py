"""
video_core — the video engine. No web server, no app imports: just "story JSON in, MP4 out".

Pipeline:
  1. Read the sections (title + content) from the performance JSON.
  2. Narrate each section sentence by sentence with Piper (so subtitles match the voice exactly),
     or use a narration file you supply.
  3. Make one picture per section (title card, or a local AI painting) plus an opening title card.
  4. Draw subtitles and a section label as transparent pictures.
  5. Animate each picture (slow zoom) with its overlays, join the clips, and mix the soundtrack:
     narration + soft tanpura + temple bell at the start + scene sounds (drums, river, flute...).

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


def scene_of(sec: dict) -> str | None:
    """First matching scene name (battle, river, ...) for a section, or None."""
    text = section_text(sec).lower()
    return next((name for name, (words, _) in SCENES.items() if any(w in text for w in words)), None)


def template_prompt(sec: dict) -> str:
    scene = scene_of(sec)
    return f"{sec.get('title', '')}, {SCENES[scene][1] if scene else DEFAULT_SCENE}, {STYLE}"


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


def label_image(text: str, path: Path) -> None:
    """Small section label in the top-left corner, e.g. '02 · STORY · The Difficult Choice'."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(CAPTION_FONTS, 22)
    w = draw.textlength(text, font=font)
    draw.rounded_rectangle([28, 26, 28 + w + 32, 26 + 40], radius=10, fill=(20, 10, 5, 150))
    draw.text((44, 34), text, font=font, fill=(255, 236, 200))
    img.save(path)


# ---------------------------------------------------------------- 4b. narration (Piper, free + offline)
# The video service speaks every caption chunk itself, so each subtitle starts exactly when its words do.
_VOICE = None
PAUSE_BETWEEN_SENTENCES = 0.30   # seconds
PAUSE_AFTER_SECTION = 0.80


def narration_available(model_path: str) -> bool:
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return bool(model_path) and Path(model_path).exists()


def _voice(model_path: str):
    global _VOICE
    if _VOICE is None:
        from piper import PiperVoice
        _VOICE = PiperVoice.load(model_path)
    return _VOICE


def narrate_section(sec: dict, index: int, work: Path, model_path: str, pace: float = 1.08):
    """Speak one section sentence by sentence.
    Returns (section_wav, duration, [(start, end, caption_text), ...])."""
    import wave
    from piper import SynthesisConfig
    voice = _voice(model_path)
    rate = voice.config.sample_rate
    config = SynthesisConfig(length_scale=pace)  # >1 = a little slower, storyteller pace
    out = work / f"voice_{index}.wav"
    timings, t = [], 0.0
    silence = lambda seconds: b"\x00\x00" * int(rate * seconds)  # 16-bit mono silence
    with wave.open(str(out), "wb") as section_wav:
        section_wav.setnchannels(1)
        section_wav.setsampwidth(2)
        section_wav.setframerate(rate)
        for j, chunk in enumerate(chunk_text(section_text(sec))):
            part = work / f"voice_{index}_{j}.wav"
            with wave.open(str(part), "wb") as w:
                voice.synthesize_wav(chunk, w, syn_config=config)
            with wave.open(str(part), "rb") as r:
                frames = r.readframes(r.getnframes())
            d = len(frames) / 2 / rate
            section_wav.writeframes(frames + silence(PAUSE_BETWEEN_SENTENCES))
            timings.append((t, t + d + PAUSE_BETWEEN_SENTENCES, chunk))  # stays up through the pause
            t += d + PAUSE_BETWEEN_SENTENCES
        section_wav.writeframes(silence(PAUSE_AFTER_SECTION))
        t += PAUSE_AFTER_SECTION
    return out, t, timings


def join_wavs(parts: list[tuple[Path | None, float]], out: Path, rate: int = 22050) -> None:
    """Concatenate WAVs in order; (None, seconds) inserts silence (e.g. under the title card)."""
    import wave
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for path, seconds in parts:
            if path is None:
                w.writeframes(b"\x00\x00" * int(rate * seconds))
            else:
                with wave.open(str(path), "rb") as r:
                    w.writeframes(r.readframes(r.getnframes()))


SCENE_SOUNDS = {"battle": "battle_drums.mp3", "storm": "storm.mp3", "river": "river.mp3", "forest": "forest.mp3",
                "temple": "temple_ambience.mp3", "divine": "krishna_flute.mp3", "emotional": "emotional_strings.mp3"}


def mix_soundtrack(voice: Path, out: Path, sounds_dir: Path | None, scene_spans: list[tuple[str | None, float, float]]) -> None:
    """Narration (loud and clear) + soft tanpura under everything + a temple bell at the start +
    each section's scene sound (drums, river, flute...) only while that section plays.
    Missing sound files are simply skipped."""
    cmd = ["ffmpeg", "-y", "-i", str(voice)]
    filters = ["[0:a]volume=1.25[v]"]
    mix = ["[v]"]
    n = 1
    if sounds_dir and (sounds_dir / "tanpura.mp3").exists():
        cmd += ["-stream_loop", "-1", "-i", str(sounds_dir / "tanpura.mp3")]
        filters.append(f"[{n}:a]volume=0.12[tan]")
        mix.append("[tan]")
        n += 1
    if sounds_dir and (sounds_dir / "temple_bell.mp3").exists():
        cmd += ["-i", str(sounds_dir / "temple_bell.mp3")]
        filters.append(f"[{n}:a]atrim=0:5,afade=t=out:st=3.5:d=1.5,volume=0.45,adelay=400:all=1[bell]")
        mix.append("[bell]")
        n += 1
    for k, (scene, start, dur) in enumerate(scene_spans):
        f = sounds_dir / SCENE_SOUNDS.get(scene or "", "") if sounds_dir else None
        if not scene or not f or not f.is_file():
            continue
        cmd += ["-stream_loop", "-1", "-i", str(f)]
        ms = int(start * 1000)
        filters.append(f"[{n}:a]atrim=0:{dur:.2f},afade=t=in:d=1,afade=t=out:st={max(0, dur - 1.5):.2f}:d=1.5,"
                       f"volume=0.16,adelay={ms}:all=1[s{k}]")
        mix.append(f"[s{k}]")
        n += 1
    filters.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[out]")
    run(cmd + ["-filter_complex", ";".join(filters), "-map", "[out]", "-ac", "2", "-ar", "44100", str(out)])


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


INTRO_SECONDS = 4.5


def render_video(data: dict, out: Path, audio: Path | None = None, *, images: str = "placeholder",
                 ai_prompts: bool = False, subtitles: bool = True, narration: str = "auto",
                 piper_model: str = "", sounds_dir: Path | None = None, sd_model: str = "stabilityai/sd-turbo",
                 ollama_url: str = "http://localhost:11434", ollama_model: str = "qwen2.5:3b",
                 progress: Progress = lambda f, m: None) -> Path:
    """The one public function: performance JSON -> MP4 at `out`.

    Sound, in order of preference:
      - `audio` given (e.g. the backend's full narration): used as-is, timing estimated from text length.
      - narration "auto"/"piper" and Piper installed: the service narrates sentence by sentence (exact sync)
        and adds tanpura, temple bell and scene sounds.
      - otherwise: silent video.
    Structure: title card -> one segment per section (picture + label + subtitles)."""
    if not shutil.which("ffmpeg"):
        raise VideoError("ffmpeg not found on PATH (Mac: brew install ffmpeg).")
    title, sections = get_sections(data)
    speak = audio is None and narration != "none" and narration_available(piper_model)
    if narration == "piper" and audio is None and not speak:
        raise VideoError("Narration needs piper-tts installed and a voice model (PIPER_MODEL).")
    work = Path(tempfile.mkdtemp(prefix="harikatha_video_"))
    try:
        n = len(sections)
        steps = n + 2
        # ---- timing + captions for every section
        voice_parts: list[tuple[Path | None, float]] = [(None, INTRO_SECONDS)]
        plan = []  # (section, duration, [(start, end, text)])
        if speak:
            for i, sec in enumerate(sections):
                progress(i / steps * 0.4, f"Narrating section {i + 1}/{n}")
                wav, dur, timings = narrate_section(sec, i, work, piper_model)
                voice_parts.append((wav, dur))
                plan.append((sec, dur, timings))
        else:
            total = audio_duration(audio) if audio else None
            for sec, dur in zip(sections, section_durations(sections, total)):
                chunks = chunk_text(section_text(sec))
                weight = sum(len(c) for c in chunks) or 1
                timings, t = [], 0.0
                for c in chunks:
                    d = dur * len(c) / weight
                    timings.append((t, t + d, c))
                    t += d
                plan.append((sec, dur, timings))

        # ---- pictures + animated clips
        base = 0.4 if speak else 0.0
        clips = []
        intro_img = work / "intro.png"
        placeholder_image({"type": "A Harikatha", "title": title}, 3, intro_img)
        clips.append(work / "clip_intro.mp4")
        ken_burns_clip(intro_img, INTRO_SECONDS, 1, clips[-1], [])
        for i, (sec, dur, timings) in enumerate(plan):
            progress(base + (i + 1) / steps * (1 - base) * 0.95, f"Picture {i + 1}/{n}: {sec.get('title', '')}")
            img = work / f"img_{i}.png"
            overlays: list[tuple[float, float, Path]] = []
            if images == "ai":
                prompt = llm_prompt(sec, ollama_url, ollama_model) if ai_prompts else template_prompt(sec)
                ai_image(prompt, img, sd_model)
                label = work / f"label_{i}.png"  # AI pictures have no text, so show which section this is
                label_image(f"{i + 1:02d} · {str(sec.get('type', '')).title()} · {sec.get('title', '')}"[:70], label)
                overlays.append((0.0, dur, label))
            else:
                placeholder_image(sec, i, img)
            if subtitles:
                for j, (a, b, text) in enumerate(timings):
                    png = work / f"cap_{i}_{j}.png"
                    caption_image(text, png)
                    overlays.append((a, b, png))
            clips.append(work / f"clip_{i}.mp4")
            ken_burns_clip(img, dur, i, clips[-1], overlays)

        # ---- join video, build soundtrack, combine
        progress(0.96, "Mixing sound and joining clips")
        (work / "clips.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
        joined = work / "joined.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "clips.txt"), "-c", "copy", str(joined)])

        soundtrack = None
        if speak:
            voice = work / "voice.wav"
            join_wavs(voice_parts, voice, rate=_voice(piper_model).config.sample_rate)
            spans, t = [], INTRO_SECONDS
            for sec, dur, _ in plan:
                spans.append((scene_of(sec), t, dur))
                t += dur
            soundtrack = work / "soundtrack.wav"
            mix_soundtrack(voice, soundtrack, sounds_dir, spans)
        elif audio:
            soundtrack = work / "soundtrack.wav"  # shift the given narration to start after the title card
            run(["ffmpeg", "-y", "-i", str(audio), "-af", f"adelay={int(INTRO_SECONDS * 1000)}:all=1",
                 "-ac", "2", "-ar", "44100", str(soundtrack)])

        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-i", str(joined)]
        cmd += (["-i", str(soundtrack), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"] if soundtrack
                else ["-c", "copy"])
        run(cmd + [str(out)])
        progress(1.0, "Done")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
