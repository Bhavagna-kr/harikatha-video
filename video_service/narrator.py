"""
narrator — an original, illustrated Harikatha storyteller who "speaks" in the corner of the video.

How the lip-sync works (simple and fully local):
  1. Measure how loud the narration voice is in every video frame (1/25 s).
  2. Quiet -> mouth closed, medium -> half open, loud -> open. Eyes blink every few seconds.
  3. Draw the character once per mouth/eye combination (6 small pictures), then tell FFmpeg
     which picture to show for how long. A gentle sway is added when it is laid over the video.

The character is drawn with code (no photo of anyone), so it is our own original design.
"""
from __future__ import annotations

import random
import wave
from array import array
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = (300, 340)          # final sprite size in pixels
S = 3                      # draw 3x bigger, then shrink = smooth edges
FPS = 25

SKIN = (176, 118, 82)
SKIN_SHADE = (150, 98, 66)
HAIR = (70, 66, 64)
GREY = (150, 146, 140)
KURTA = (246, 236, 214)
SHAWL = (214, 120, 40)
SHAWL_EDGE = (176, 32, 40)
GOLD = (214, 170, 80)
MAROON = (70, 22, 20)


def _p(*xy: float) -> list[float]:
    """Scale coordinates from the 300x340 design grid to the big drawing canvas."""
    return [v * S for v in xy]


def draw_narrator(mouth: str = "closed", eyes: str = "open") -> Image.Image:
    """mouth: closed | half | open ; eyes: open | shut"""
    W, H = SIZE
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Body: cream kurta, saffron shawl over the left shoulder, marigold garland.
    d.ellipse(_p(40, 245, 260, 420), fill=KURTA)
    d.polygon(_p(70, 262, 120, 250, 205, 420, 140, 420), fill=SHAWL)
    d.line(_p(120, 250, 205, 420), fill=SHAWL_EDGE, width=6 * S)
    for k in range(13):  # garland: a U of orange flowers
        t = k / 12
        x = 105 + 90 * t
        y = 262 + 70 * (1 - (2 * t - 1) ** 2)
        d.ellipse(_p(x - 9, y - 9, x + 9, y + 9), fill=(240, 140, 30) if k % 2 else (250, 180, 40))

    # Neck and ears.
    d.rectangle(_p(128, 215, 172, 262), fill=SKIN_SHADE)
    d.ellipse(_p(86, 140, 108, 180), fill=SKIN_SHADE)
    d.ellipse(_p(192, 140, 214, 180), fill=SKIN_SHADE)

    # Head with greying hair at the sides and a dark top.
    d.ellipse(_p(98, 78, 202, 232), fill=SKIN)
    d.chord(_p(96, 74, 204, 150), 180, 360, fill=HAIR)
    d.pieslice(_p(92, 100, 118, 170), 90, 270, fill=GREY)
    d.pieslice(_p(182, 100, 208, 170), 270, 90, fill=GREY)

    # Tilak (namam): white U with a red line, on the forehead.
    d.line(_p(141, 117, 144, 141), fill=(250, 250, 245), width=3 * S)
    d.line(_p(159, 117, 156, 141), fill=(250, 250, 245), width=3 * S)
    d.line(_p(150, 115, 150, 143), fill=(205, 30, 40), width=4 * S)

    # Eyebrows and eyes.
    d.arc(_p(116, 132, 144, 146), 200, 340, fill=HAIR, width=3 * S)
    d.arc(_p(156, 132, 184, 146), 200, 340, fill=HAIR, width=3 * S)
    for cx in (130, 170):
        if eyes == "open":
            d.ellipse(_p(cx - 11, 146, cx + 11, 160), fill=(250, 246, 238))
            d.ellipse(_p(cx - 5, 147, cx + 5, 159), fill=(45, 30, 25))
        else:
            d.arc(_p(cx - 11, 146, cx + 11, 160), 20, 160, fill=(60, 40, 30), width=3 * S)

    # Nose.
    d.line(_p(150, 152, 146, 180), fill=SKIN_SHADE, width=3 * S)
    d.arc(_p(140, 174, 160, 186), 20, 160, fill=SKIN_SHADE, width=3 * S)

    # Mouth (the part that moves), then the moustache drawn over its top edge.
    if mouth == "closed":
        d.arc(_p(136, 194, 164, 206), 20, 160, fill=(110, 50, 40), width=3 * S)
    elif mouth == "half":
        d.ellipse(_p(138, 196, 162, 210), fill=(90, 30, 30))
    else:
        d.ellipse(_p(136, 194, 164, 220), fill=(80, 25, 25))
        d.ellipse(_p(142, 208, 158, 219), fill=(200, 90, 90))  # tongue
    d.chord(_p(124, 184, 176, 204), 180, 360, fill=GREY)

    # Shrink for smooth edges, then frame it as a round medallion with a gold ring.
    img = img.resize(SIZE, Image.LANCZOS)
    badge = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    cx, cy, r = W // 2, H // 2 + 10, 140
    bd.ellipse([cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6], fill=GOLD)
    bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MAROON)
    mask = Image.new("L", SIZE, 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    clipped = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    clipped.paste(img, (0, 0), Image.composite(img.getchannel("A"), Image.new("L", SIZE, 0), mask))
    badge.alpha_composite(clipped)
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([cx - r, cy - r + 8, cx + r, cy + r + 8], fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    shadow.alpha_composite(badge)
    return shadow


# ---------------------------------------------------------------- lip-sync
def loudness_per_frame(wav_path: Path, fps: int = FPS) -> list[float]:
    """Average loudness (RMS) of the voice for every video frame, scaled so the loudest speech ≈ 1."""
    with wave.open(str(wav_path), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        if width != 2:
            raise ValueError("expected 16-bit audio")
        samples = array("h", w.readframes(w.getnframes()))
    if channels > 1:
        samples = samples[::channels]
    step = max(1, rate // fps)
    levels = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step:4]  # every 4th sample is plenty for a loudness estimate
        levels.append((sum(x * x for x in chunk) / max(1, len(chunk))) ** 0.5)
    peak = sorted(levels)[int(len(levels) * 0.95)] or 1.0  # 95th percentile, ignores rare spikes
    return [min(1.0, v / peak) for v in levels]


def mouth_track(levels: list[float]) -> list[str]:
    """Turn loudness into mouth shapes, holding each shape at least 2 frames so it doesn't flicker."""
    shapes, last, held = [], "closed", 0
    for v in levels:
        want = "closed" if v < 0.12 else ("half" if v < 0.45 else "open")
        if want != last and held < 2:
            want = last
        held = held + 1 if want == last else 1
        last = want
        shapes.append(want)
    return shapes


def build_narrator_video(voice_wav: Path, work: Path, total_seconds: float) -> Path:
    """Make a transparent .mov of the narrator talking along with `voice_wav`."""
    frames = int(total_seconds * FPS) + 1
    mouths = mouth_track(loudness_per_frame(voice_wav))
    mouths += ["closed"] * max(0, frames - len(mouths))
    rng = random.Random(7)  # fixed seed: same video every time
    eyes, next_blink, i = ["open"] * frames, rng.randint(60, 120), 0
    while next_blink < frames:  # a blink (4 frames) every 2.5-5 seconds
        for k in range(next_blink, min(frames, next_blink + 4)):
            eyes[k] = "shut"
        next_blink += rng.randint(60, 125)

    sprites: dict[tuple[str, str], Path] = {}
    for m in ("closed", "half", "open"):
        for e in ("open", "shut"):
            path = work / f"narrator_{m}_{e}.png"
            draw_narrator(m, e).save(path)
            sprites[(m, e)] = path

    # FFmpeg "concat" list: show each picture for as long as the state lasts.
    lines, state, run_len = [], (mouths[0], eyes[0]), 0
    for f in range(frames):
        cur = (mouths[f], eyes[f])
        if cur != state:
            lines.append(f"file '{sprites[state]}'\nduration {run_len / FPS:.4f}\n")
            state, run_len = cur, 0
        run_len += 1
    lines.append(f"file '{sprites[state]}'\nduration {run_len / FPS:.4f}\nfile '{sprites[state]}'\n")
    listing = work / "narrator.txt"
    listing.write_text("".join(lines), encoding="utf-8")
    return listing
