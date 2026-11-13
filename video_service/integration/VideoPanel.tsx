// Drop-in "Make Video" panel for the Harikatha frontend.
// Copy this file to frontend/src/VideoPanel.tsx — see integration/INTEGRATION.md.
//
// It only needs the performance object the app already has (+ the narration URL if one exists),
// and talks to the separate video service on port 8001. No backend changes are required.
import { useEffect, useRef, useState } from 'react'

const VIDEO_API = 'http://localhost:8001'

type Job = { job_id: string; status: 'queued' | 'running' | 'done' | 'failed'; progress: number; message: string; video_url: string | null }

export default function VideoPanel({ performance }: { performance: unknown }) {
  const [images, setImages] = useState<'placeholder' | 'ai'>('placeholder')
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState('')
  const timer = useRef<number | null>(null)

  // A new story means any old video no longer matches it.
  useEffect(() => { setJob(null); setError('') }, [performance])
  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current) }, [])

  async function start() {
    setError('')
    try {
      const res = await fetch(`${VIDEO_API}/api/video/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // No audio_url: the service narrates sentence by sentence itself (exact subtitle + lip timing).
        // Pass audio_url only if you want the backend's own narration file instead.
        body: JSON.stringify({ performance, images, ai_prompts: images === 'ai', narration: 'auto', narrator: true })
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Could not start the video')
      setJob({ job_id: data.job_id, status: 'queued', progress: 0, message: 'Waiting', video_url: null })
      // Videos take minutes, so check progress every 1.5 seconds.
      timer.current = window.setInterval(async () => {
        const j: Job = await (await fetch(`${VIDEO_API}/api/video/jobs/${data.job_id}`)).json()
        setJob(j)
        if (j.status === 'done' || j.status === 'failed') window.clearInterval(timer.current!)
      }, 1500)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Video service not reachable on port 8001')
    }
  }

  const busy = job?.status === 'queued' || job?.status === 'running'
  const videoSrc = job?.status === 'done' && job.video_url ? `${VIDEO_API}${job.video_url}` : null

  return (
    <div className="audio-tools" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 10 }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <select value={images} onChange={e => setImages(e.target.value as 'placeholder' | 'ai')} disabled={busy}>
          <option value="placeholder">Title cards (fast)</option>
          <option value="ai">AI paintings (slower)</option>
        </select>
        <button className="audio-button" onClick={start} disabled={busy}>
          {busy ? `Making video… ${Math.round((job?.progress ?? 0) * 100)}%` : 'Make Video'}
        </button>
      </div>
      {busy && <small>{job?.message}</small>}
      {job?.status === 'failed' && <small style={{ color: '#a33' }}>{job.message}</small>}
      {error && <small style={{ color: '#a33' }}>{error}</small>}
      {videoSrc && (
        <>
          <video controls src={videoSrc} style={{ width: '100%', borderRadius: 10 }} />
          <a className="secondary-button" href={videoSrc} download>Download video</a>
        </>
      )}
    </div>
  )
}
