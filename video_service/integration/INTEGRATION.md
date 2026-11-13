# Adding the video feature to the main app

The video service is separate (port 8001). The **only** things the main app must provide:

1. The performance object: `{ "title": "...", "sections": [ { "type", "title", "content" }, ... ] }`
   (a full saved record `{ "performance": {...} }` also works).
2. Nothing else: the service narrates the story itself (Piper), adds music and the animated narrator.
   (Optionally it can use the backend's narration WAV via `audio_url` instead.)

If the team renames fields, only `get_sections()` in `video_core.py` needs to change.

## Frontend — 3 small edits

1. Copy `integration/VideoPanel.tsx` to `frontend/src/VideoPanel.tsx`.
2. At the top of `App.tsx`:
   ```tsx
   import VideoPanel from './VideoPanel'
   ```
3. Where the performance is shown (in the current App.tsx: right after the full-narration
   `<div className="audio-tools">…</div>`):
   ```tsx
   <VideoPanel performance={result.performance} />
   ```

No backend changes are needed.

## API (for any other frontend)

| Method | Path | Body / result |
|---|---|---|
| POST | `/api/video/jobs` | `{ performance, images: "placeholder" \| "ai", ai_prompts?, narration?: "auto" \| "none", narrator?: bool, subtitles?, audio_url? }` → `{ job_id }` |
| GET | `/api/video/jobs/{job_id}` | `{ status: queued \| running \| done \| failed, progress: 0..1, message, video_url }` |
| GET | `/api/video/jobs/{job_id}/file` | the MP4 |
| GET | `/api/video/health` | `{ ffmpeg, ai_images }` |

Interactive docs: http://localhost:8001/docs
