import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ago, api, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'

/**
 * The bell (D114): what needs this person, and what changed in their own work.
 *
 * "To do" items ask for action and stay at the top until the work is done — by
 * this person or anyone else, which the item then says. Everything else is
 * good-to-know and drops into the list. New to-do items also pop up briefly in
 * the corner, so a person on another page still sees an approval or a returned
 * finding the moment it arrives.
 *
 * It reads every ten seconds rather than holding a second live stream open per
 * tab: administrators may not use the case stream at all, and one cheap indexed
 * read is simpler than two connections. Reading something is not doing it, so
 * "mark all read" never clears the to-do count.
 */

const POLL_MS = 10000

const KIND = {
  CASE_ASSIGNED: { icon: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M5 21v-3a7 7 0 0 1 14 0v3', tone: 'act' },
  CASE_REASSIGNED_AWAY: { icon: 'M5 12h14M13 6l6 6-6 6', tone: 'info' },
  APPROVAL_WAITING: { icon: 'M20 6 9 17l-5-5', tone: 'act' },
  OUTCOME_APPROVED: { icon: 'M20 6 9 17l-5-5', tone: 'good' },
  OUTCOME_SENT_BACK: { icon: 'M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3', tone: 'act' },
  OUTCOME_REJECTED: { icon: 'M18 6 6 18M6 6l12 12', tone: 'act' },
  ESCALATION_WAITING: { icon: 'M12 19V5M5 12l7-7 7 7', tone: 'act' },
  CASE_HANDED_BACK: { icon: 'M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3', tone: 'act' },
  CASE_CLOSED: { icon: 'M4 5h16v4H4zM6 9v10h12V9', tone: 'good' },
  NOTE_ADDED: { icon: 'M4 4h16v12H8l-4 4z', tone: 'info' },
  CUSTOMER_REPORTED: { icon: 'M12 9v4M12 17h.01M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z', tone: 'act' },
  CLOCK_BREACHED: { icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2', tone: 'act' },
  BANK_ACTION_DONE: { icon: 'M20 6 9 17l-5-5', tone: 'good' },
  BANK_ACTION_FAILED: { icon: 'M12 9v4M12 17h.01M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z', tone: 'act' },
  CHANGE_WAITING: { icon: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3', tone: 'act' },
  CHANGE_DECIDED: { icon: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3', tone: 'info' },
}

function Glyph({ d, size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {d.split(/(?=M)/).map((p, i) => <path key={i} d={p} />)}
    </svg>
  )
}

function Item({ n, onOpen }) {
  const k = KIND[n.kind] || { icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', tone: 'info' }
  const open = n.urgent && !n.resolved_at
  return (
    <li className={`notif ${n.read_at ? '' : 'unread'} ${open ? 'todo' : ''}`}>
      <button type="button" className="notif-hit" onClick={() => onOpen(n)} disabled={!n.link && !!n.read_at}>
        <span className={`notif-icon ${k.tone}`}><Glyph d={k.icon} /></span>
        <span className="notif-text">
          <span className="notif-title">{n.title}{n.times > 1 && <span className="notif-times"> · {n.times} updates</span>}</span>
          {n.body && <span className="notif-body">{n.body}</span>}
          <span className="notif-meta">
            <span title={when(n.updated_at)}>{ago(n.updated_at)}</span>
            {open && <span className="notif-flag">To do</span>}
            {n.urgent && n.resolved_at && <span className="notif-done">Done · {n.resolved_note}</span>}
            {!n.link && n.case_id && <span>No longer yours to open</span>}
          </span>
        </span>
      </button>
    </li>
  )
}

export default function NotificationBell() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [toasts, setToasts] = useState([])
  const seen = useRef(null)          // ids already known; null until the first read
  const panel = useRef(null)
  const loader = useCallback(() => api.notifications(40), [])
  const feed = usePolling(loader, POLL_MS)
  const data = feed.data
  const refresh = feed.refresh

  // Pop up "to do" items that arrived since the last read — never the backlog
  // that was already there when the page loaded.
  useEffect(() => {
    if (!data) return
    const ids = new Set(data.items.map((n) => n.id))
    if (seen.current) {
      const fresh = data.items.filter((n) => !seen.current.has(n.id) && n.urgent && !n.resolved_at && !n.read_at)
      if (fresh.length) setToasts((t) => [...fresh.slice(0, 3), ...t].slice(0, 3))
    }
    seen.current = new Set([...(seen.current || []), ...ids])
  }, [data])

  useEffect(() => {
    if (!toasts.length) return undefined
    const timer = setTimeout(() => setToasts((t) => t.slice(0, -1)), 9000)
    return () => clearTimeout(timer)
  }, [toasts])

  // Close on Escape or a click outside, and refresh when the tab comes back.
  useEffect(() => {
    const key = (e) => e.key === 'Escape' && setOpen(false)
    const click = (e) => panel.current && !panel.current.contains(e.target) && setOpen(false)
    const focus = () => refresh()
    document.addEventListener('keydown', key)
    document.addEventListener('mousedown', click)
    window.addEventListener('focus', focus)
    return () => {
      document.removeEventListener('keydown', key)
      document.removeEventListener('mousedown', click)
      window.removeEventListener('focus', focus)
    }
  }, [refresh])

  const openItem = async (n) => {
    setToasts((t) => t.filter((x) => x.id !== n.id))
    if (!n.read_at) {
      try { await api.readNotification(n.id) } catch { /* the next read shows the truth */ }
      feed.refresh()
    }
    if (n.link) { setOpen(false); navigate(n.link) }
  }
  const readAll = async () => {
    try { await api.readAllNotifications() } finally { feed.refresh() }
  }

  const unread = data?.unread ?? 0
  const toDo = data?.to_do ?? 0
  const items = data?.items || []
  const label = `Notifications: ${unread} unread${toDo ? `, ${toDo} to do` : ''}`

  return (
    <div className="notif-wrap" ref={panel}>
      <button type="button" className={`notif-bell ${toDo ? 'has-todo' : ''}`} aria-label={label} title={label}
              aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        <Glyph d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" size={18} />
        {unread > 0 && <span className="notif-badge">{unread > 99 ? '99+' : unread}</span>}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="Notifications">
          <div className="notif-head">
            <div>
              <strong>Notifications</strong>
              <span className="notif-sub">{toDo ? `${toDo} need${toDo === 1 ? 's' : ''} you` : 'Nothing waiting for you'}</span>
            </div>
            <button type="button" className="ghost notif-readall" onClick={readAll} disabled={!unread}>Mark all read</button>
          </div>
          {feed.error && !data && <p className="notif-empty">Notifications could not be loaded. {feed.error}</p>}
          {data && !items.length && <p className="notif-empty">Nothing yet. You will hear here when a case is assigned to you,
            when your work is approved or sent back, and when something waits for your decision.</p>}
          <ul className="notif-list">
            {items.map((n) => <Item key={n.id} n={n} onOpen={openItem} />)}
          </ul>
        </div>
      )}

      <div className="notif-toasts" aria-live="polite">
        {toasts.map((n) => (
          <div key={n.id} className="notif-toast" role="status">
            <span className={`notif-icon ${(KIND[n.kind] || {}).tone || 'act'}`}><Glyph d={(KIND[n.kind] || KIND.CASE_ASSIGNED).icon} /></span>
            <div className="notif-text">
              <span className="notif-title">{n.title}</span>
              {n.body && <span className="notif-body">{n.body}</span>}
              <span className="notif-toast-actions">
                {n.link && <button type="button" className="primary" onClick={() => openItem(n)}>Open</button>}
                <button type="button" className="ghost" onClick={() => setToasts((t) => t.filter((x) => x.id !== n.id))}>Dismiss</button>
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
