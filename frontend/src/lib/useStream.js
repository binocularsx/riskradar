import { useEffect, useRef, useState } from 'react'

/**
 * Live alert stream (FR-030, FR-031).
 *
 * `EventSource` is doing the hard part. It reconnects on its own and replays the
 * gap by sending `Last-Event-ID`, which the server resumes from — that protocol-
 * level replay is the reason SSE was chosen over WebSockets (D14), where a
 * dropped socket simply loses whatever arrived while it was down.
 *
 * There is no polling fallback here and none is needed: the browser's own
 * reconnect logic is the fallback, and the durable `stream_events` table means
 * a reconnect never misses anything.
 */
export function useAlertStream({ enabled = true, onAlert, onAlarm } = {}) {
  const [connected, setConnected] = useState(false)
  const [lastEventId, setLastEventId] = useState(null)
  const handlers = useRef({ onAlert, onAlarm })
  handlers.current = { onAlert, onAlarm }

  useEffect(() => {
    if (!enabled) return undefined

    const source = new EventSource('/v1/stream', { withCredentials: true })

    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)

    source.addEventListener('alert', (event) => {
      setLastEventId(event.lastEventId)
      try {
        handlers.current.onAlert?.(JSON.parse(event.data))
      } catch {
        /* a malformed frame must not kill the stream */
      }
    })

    source.addEventListener('alarm', (event) => {
      setLastEventId(event.lastEventId)
      try {
        handlers.current.onAlarm?.(JSON.parse(event.data))
      } catch {
        /* ignore */
      }
    })

    return () => source.close()
  }, [enabled])

  return { connected, lastEventId }
}
