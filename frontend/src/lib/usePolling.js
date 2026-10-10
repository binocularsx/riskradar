import { useCallback, useEffect, useRef, useState } from 'react'

// Keep the last successful read, but explicitly report when it is stale.
// The caller supplies a stable loader (an api method or useCallback).
export function usePolling(loader, interval = 20000, enabled = true) {
  const [state, setState] = useState({ data: null, error: null, updatedAt: null, loading: true })
  const refreshRef = useRef(() => {})
  const refresh = useCallback(() => refreshRef.current(), [])
  useEffect(() => {
    let alive = true
    let running = false
    setState({ data: null, error: null, updatedAt: null, loading: enabled })
    const load = async () => {
      if (!enabled || running) return
      running = true
      if (alive) setState((s) => ({ ...s, loading: true }))
      try {
        const data = await loader()
        if (alive) setState({ data, error: null, updatedAt: new Date(), loading: false })
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e.message, loading: false }))
      } finally { running = false }
    }
    refreshRef.current = load
    load()
    const timer = enabled && interval ? setInterval(load, interval) : null
    return () => { alive = false; clearInterval(timer); refreshRef.current = () => {} }
  }, [loader, interval, enabled])
  return { ...state, refresh }
}
