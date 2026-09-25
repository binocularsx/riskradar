import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, clock, nairaShort } from '../lib/api'
import { useAlertStream } from '../lib/useStream'
import { Banner, RiskBadge } from '../components/ui'

/**
 * Case Queue (Figma "Case Queue"): the desk state across the top, then a full
 * table of cases ordered by severity, money and lateness. "Take next case" asks
 * the server for the highest-priority unassigned case and puts the analyst's
 * name on it (D46); a row opens the case detail.
 */

const SCOPES = [
  { key: 'mine', label: 'Mine' },
  { key: 'all', label: 'Everything' },
  { key: 'unassigned', label: 'Unassigned' },
  { key: 'breaching', label: 'Past due' },
  { key: 'escalated', label: 'Escalated' },
  { key: 'awaiting_close', label: 'Awaiting close' },
]

export default function CaseQueue({ user }) {
  const canReview = user.permissions.includes('cases:review')
  // A lead holds cases:review too, but taking a case off the queue is the
  // analyst's job. Offering it here made the supervisor a queue worker.
  const supervises = user.permissions.includes('cases:close')
  const [scope, setScope] = useState(canReview ? 'mine' : 'all')
  const [q, setQ] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [alarms, setAlarms] = useState([])
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  const load = useCallback(async () => {
    try { setData(await api.worklist({ scope, limit: 100 })); setError(null) }
    catch (e) { setError(e.message) }
  }, [scope])

  useEffect(() => { load() }, [load])
  const { connected } = useAlertStream({
    onAlert: load,
    onAlarm: (a) => setAlarms((prev) => [a, ...prev].slice(0, 3)),
    onCaseNews: load,
  })

  const takeNext = useCallback(async () => {
    setBusy(true)
    try {
      const { case: next } = await api.nextCase()
      if (!next) { setError('Nothing waiting — the queue is clear.'); return }
      navigate(`/cases/${next.id}`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }, [navigate])

  const s = data?.summary
  const items = (data?.items ?? []).filter((c) => {
    if (!q.trim()) return true
    const t = q.toLowerCase()
    return (c.customer_name || '').toLowerCase().includes(t) || String(c.id).includes(t)
  })

  const assignedLabel = (c) =>
    c.assignee_id == null ? 'Unassigned' : c.assignee_id === user.id ? 'You' : 'Assigned'

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Case Queue</h1>
          <p className="page-sub">Cases ordered by severity, exposure and how late they are.</p>
        </div>
        {canReview && !supervises && (
          <button className="primary" onClick={takeNext} disabled={busy}>
            {busy ? 'Finding…' : '＋ Take next case'}
          </button>
        )}
      </div>

      {[...new Map(alarms.map((a) => [`${a.code}:${a.detail}`, a])).values()].map((a, i) => (
        <Banner key={i} kind="warn"><strong>System alarm — {a.code}.</strong> {a.detail}</Banner>
      ))}
      {error && <Banner kind="error">{error}</Banner>}

      <div className="statrow">
        <Stat label="In queue" value={s?.open_cases ?? '—'} />
        <Stat label="Money at risk" value={s ? nairaShort(s.total_exposure_minor) : '—'} tone="accent" />
        <Stat label="Past due" value={s?.breaching ?? '—'} tone={s?.breaching ? 'danger' : ''} />
        <Stat label="Unassigned" value={s?.unassigned ?? '—'} tone={s?.unassigned ? 'warn' : ''} />
        <Stat label="With me" value={s?.mine ?? '—'} />
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="toolbar">
          <div className="segmented">
            {SCOPES.map((sc) => (
              <button key={sc.key} className={scope === sc.key ? 'on' : ''} onClick={() => setScope(sc.key)}>
                {sc.label}
              </button>
            ))}
          </div>
          <div className="row" style={{ gap: 10 }}>
            <span className="live"><span className={`live-dot ${connected ? 'on' : 'off'}`} />
              {connected ? 'Live' : 'Reconnecting'}</span>
            <div className="searchbar sm">
              <svg className="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
                <circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" strokeLinecap="round" /></svg>
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search case or customer…" />
            </div>
          </div>
        </div>

        <div className="table-scroll">
          <table className="rowtable">
            <thead>
              <tr>
                <th>Severity</th><th>Case</th><th>Customer</th><th className="num">Exposure</th>
                <th className="num">Alerts</th><th>Recommendation</th><th>SLA</th><th>Assigned</th><th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => navigate(`/cases/${c.id}`)}>
                  <td><RiskBadge level={c.risk_level} /></td>
                  <td className="mono">CASE-{c.id}</td>
                  <td>
                    {c.customer_name || 'Unknown customer'}
                    <div className="cellflags">
                      {c.handling === 'MACHINE' && <span className="pill suppress">machine action</span>}
                      {c.watchlisted && <span className="pill escalate">flagged</span>}
                      {c.industry_flagged && <span className="pill override">flagged elsewhere</span>}
                    </div>
                  </td>
                  <td className="num" style={{ fontWeight: 600 }}>{nairaShort(c.exposure_minor)}</td>
                  <td className="num">{c.alert_count}</td>
                  <td className="reco-cell">{c.recommendation?.action || '—'}</td>
                  <td><span className={`sla sla-${c.sla_state}`}>{clock(c.sla_remaining_minutes)}</span></td>
                  <td className={c.assignee_id === user.id ? '' : 'dim'}>{assignedLabel(c)}</td>
                  <td className="num"><span className="link">Open →</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!items.length && (
            <div className="empty"><div className="big">Queue clear</div>Nothing matches this filter.</div>
          )}
        </div>
        {items.length > 0 && (
          <div className="table-foot dim">Showing {items.length} case{items.length === 1 ? '' : 's'}
            {scope === 'mine' && s?.unassigned ? ` · ${s.unassigned} more in the shared pool` : ''}.</div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, tone = '' }) {
  return (
    <div className="statcard">
      <div className="statcard-k">{label}</div>
      <div className={`statcard-v ${tone}`}>{value}</div>
    </div>
  )
}
