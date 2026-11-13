# Harikatha Video Service

A separate service (port **8001**) that turns a generated performance into a narrated MP4:

- opening title card with a temple bell
- one picture per section (title card or AI painting) with a slow zoom and a section label
- narration spoken sentence by sentence with Piper, so subtitles match the voice exactly
- soft tanpura underneath, plus scene sounds (battle drums, river, flute, strings) per section
- an original animated storyteller in the corner whose mouth moves with the voice (`narrator.py`)

It never imports the main backend, so the team can keep changing the backend and frontend freely.
The only contract is the performance JSON: `{ title, sections: [{ type, title, content }] }`.

```
 Main app (5173 / 8000)                    Video service (8001)
 ─────────────────────                     ─────────────────────────────
 performance JSON  ──── POST /api/video/jobs ───▶  queue ─▶ video_core.render_video
 narration URL (opt)                                         ├─ pictures (cards or AI)
 polls progress   ◀─── GET  /api/video/jobs/{id} ─           ├─ subtitles
 plays the MP4    ◀─── GET  /api/video/jobs/{id}/file        └─ ffmpeg: zoom + join + audio
```

All free and local: Piper for the voice, Ollama for picture prompts (optional), SD-Turbo for AI paintings (optional).
Voice model and sounds default to `../backend/voices` and `../backend/sounds`; override with `PIPER_MODEL` / `SOUNDS_DIR`.

## Files
| File | What it is |
|---|---|
| `video_core.py` | The engine. `render_video(performance, out, ...)` — no web code. |
| `narrator.py` | Draws the storyteller and turns voice loudness into mouth movements. |
| `server.py` | FastAPI wrapper: job queue + progress. |
| `test_page.html` | Standalone demo page at http://localhost:8001 |
| `make_video.py` | Command-line version. |
| `integration/` | Drop-in React component + 3-step guide for the main frontend. |

## Mac setup (once)
```bash
cd video_service
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# optional, for AI paintings (~2-3 GB model downloads on first use):
.venv/bin/python -m pip install -r requirements-ai.txt
```

## Run
```bash
cd video_service
.venv/bin/python -m uvicorn server:app --reload --port 8001
```
Open http://localhost:8001 (test page) or http://localhost:8001/docs (API).

Command line instead: `.venv/bin/python make_video.py performance.json --out video.mp4`
