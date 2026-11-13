"""Command-line version of the video service (handy for testing without any server).

  python make_video.py performance.json --out harikatha.mp4
  python make_video.py performance.json --audio narration.wav --images ai --ai-prompts --out harikatha.mp4
"""
import argparse
import json
import sys
from pathlib import Path

import video_core


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("performance", type=Path)
    ap.add_argument("--audio", type=Path)
    ap.add_argument("--out", type=Path, default=Path("harikatha.mp4"))
    ap.add_argument("--images", choices=["placeholder", "ai"], default="placeholder")
    ap.add_argument("--ai-prompts", action="store_true", help="let the local LLM write the picture prompts")
    ap.add_argument("--no-subtitles", action="store_true")
    ap.add_argument("--no-narration", action="store_true", help="silent video (no Piper voice)")
    here = Path(__file__).resolve().parent
    ap.add_argument("--piper-model", default=str(here.parent / "backend" / "voices" / "en_US-lessac-medium.onnx"))
    ap.add_argument("--sounds", type=Path, default=here.parent / "backend" / "sounds")
    args = ap.parse_args()
    try:
        data = json.loads(args.performance.read_text(encoding="utf-8"))
        out = video_core.render_video(
            data, args.out.resolve(), args.audio.resolve() if args.audio else None, images=args.images,
            ai_prompts=args.ai_prompts, subtitles=not args.no_subtitles,
            narration="none" if args.no_narration else "auto", piper_model=args.piper_model,
            sounds_dir=args.sounds if args.sounds.is_dir() else None,
            progress=lambda f, m: print(f"[{f * 100:5.1f}%] {m}"))
    except video_core.VideoError as exc:
        sys.exit(f"Error: {exc}")
    print(f"Done → {out}")


if __name__ == "__main__":
    main()
