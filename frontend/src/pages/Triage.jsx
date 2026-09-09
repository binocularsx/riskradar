import { useCallback, useEffect, useState } from 'react'

import { api, clock, nairaShort } from '../lib/api'
import { useAlertStream } from '../lib/useStream'
import { Banner, RiskBadge } from '../components/ui'
import CaseView from '../components/CaseView'

/**
 * The triage workspace — the screen an analyst lives on.
 *
 * The list is on the left and the case is on the right, permanently, so
 * deciding never costs a page load and the analyst never loses their place. The
 * strip along the top is the state of the desk, not of the system: open cases,
 * money at risk, how many clocks have run out.
 *
 * "Start reviewing" is the intended way in. It asks the server for the
 * highest-priority unassigned case and puts the analyst's name on it, so two
 * people cannot work the same case and "cases per analyst per day" becomes a
 * real number instead of an estimate.
 */

const SCOPES = [
  { key: 'all', label: 'Everything' },
  { key: 'mine', label: 'Mine' },
  { key: 'unassigned', label: 'Unassigned' },
  { key: 'breaching', label: 'Past due' },
]

export default function Triage({ user }) {
  // An analyst's default view is their own work, not the whole bank's. "Am I
  // handling everything or just my cases?" should be answered by what loads,
  // not by reading a filter chip.
  const [scope, setScope] = useState(
    user.permissions.includes('cases:review') ? 'mine' : 'all'
  )
  const [data, setData] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState(null)
  const [alarms, setAlarms] = useState([])
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (keepSelection = true) => {
    try {
      const d = await api.worklist({ scope, limit: 80 })
      setData(d)
      setError(null)
      setSelectedId((current) => {
        if (keepSelection && current && d.items.some((c) => c.id === current)) return current
        return d.items[0]?.id ?? null
      })
    } catch (e) {
      setError(e.message)
    }
  }, [scope])

  useEffect(() => { load(false) }, [load])

  // New alerts change the pile, so the pile refreshes. Alarms are shown, never
  // swallowed — a model outage must not be something you find out later.
  const { connected } = useAlertStream({
    onAlert: () => load(true),
    onAlarm: (a) => setAlarms((prev) => [a, ...prev].slice(0, 3)),
  })

  const startReviewing = useCallback(async () => {
    setBusy(true)
    try {
      const { case: next } = await api.nextCase()
      if (!next) { setError('Nothing waiting — the queue is clear.'); return }
      await load(false)
      setSelectedId(next.id)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [load])

  const items = data?.items ?? []
  const selected = items.find((c) => c.id === selectedId) ?? null
  const s = data?.summary

  const advance = useCallback(() => {
    const i = items.findIndex((c) => c.id === selectedId)
    setSelectedId(items[i + 1]?.id ?? items[0]?.id ?? null)
  }, [items, selectedId])

  return (
    <>
      <div className="deskbar">
        <div className="deskstat">
          <div className="badge">◎</div>
          <div className="k">Open cases</div>
          <div className="v">{s?.open_cases ?? '—'}</div>
        </div>
        <div className="deskstat money">
          <div className="badge">₦</div>
          <div className="k">Money at risk</div>
          <div className="v">{s ? nairaShort(s.total_exposure_minor) : '—'}</div>
        </div>
        <div className={`deskstat ${s?.breaching ? 'alarm' : ''}`}>
          <div className="badge">!</div>
          <div className="k">Past due</div>
          <div className="v">{s?.breaching ?? '—'}</div>
        </div>
        <div className="deskstat">
          <div className="badge">◇</div>
          <div className="k">Unassigned</div>
          <div className="v">{s?.unassigned ?? '—'}</div>
        </div>
        <div className="deskstat">
          <div className="badge">✓</div>
          <div className="k">With me</div>
          <div className="v">{s?.mine ?? '—'}</div>
        </div>
        <div style={{ flex: 1 }} />
        <div className="row">
          <span className="live">
            <span className={`live-dot ${connected ? 'on' : 'off'}`} />
            {connected ? 'live' : 'reconnecting'}
          </span>
          <button className="primary big" onClick={startReviewing} disabled={busy}>
            {busy ? 'Finding…' : 'Start reviewing'}
          </button>
        </div>
      </div>

      <div className="workspace">
        <aside className="worklist">
          <div className="worklist-head">
            <div className="segmented">
              {SCOPES.map((sc) => (
                <button key={sc.key}
                        className={scope === sc.key ? 'on' : ''}
                        onClick={() => setScope(sc.key)}>
                  {sc.label}
                </button>
              ))}
            </div>
            <div className="dim" style={{ fontSize: 11.5, marginTop: 9, lineHeight: 1.45 }}>
              {scope === 'mine' ? (
                <>
                  <strong style={{ color: 'var(--text-2)' }}>
                    Your {items.length} case{items.length === 1 ? '' : 's'}.
                  </strong>{' '}
                  {s?.unassigned ?? 0} more are waiting in the shared pool —
                  press <strong>Start reviewing</strong> to take the next one.
                </>
              ) : scope === 'unassigned' ? (
                <>Nobody is working these yet. Taking one assigns it to you.</>
              ) : scope === 'breaching' ? (
                <>Past their target response time. Oldest and largest first.</>
              ) : (
                <>Every open case on the desk, whoever owns it.</>
              )}
              <br />
              Ordered by severity, then money at risk, then how late it is.
            </div>
          </div>

          <div className="worklist-scroll">
            {items.map((c) => (
              <button key={c.id}
                      className={`wl-item ${c.id === selectedId ? 'active' : ''}`}
                      onClick={() => setSelectedId(c.id)}>
                <div className="wl-top">
                  <span className="wl-money">{nairaShort(c.exposure_minor)}</span>
                  <span className={`sla sla-${c.sla_state}`}>{clock(c.sla_remaining_minutes)}</span>
                </div>
                <div className="wl-name">{c.customer_name || 'Unknown customer'}</div>
                <div className="wl-meta">
                  <RiskBadge level={c.risk_level} />
                  <span className="tag">{c.alert_count} alert{c.alert_count === 1 ? '' : 's'}</span>
                  {c.distinct_beneficiaries > 3 && (
                    <span className="tag">{c.distinct_beneficiaries} payees</span>
                  )}
                  {c.new_device && <span className="tag">new device</span>}
                  {c.assignee_id === user.id && <span className="pill">mine</span>}
                </div>
              </button>
            ))}
            {!items.length && (
              <div className="empty">
                <div className="big">Queue clear</div>
                Nothing matches this filter.
              </div>
            )}
          </div>
        </aside>

        <section className="casecol">
          {alarms.map((a, i) => (
            <Banner key={i} kind="warn">
              <strong>System alarm — {a.code}.</strong> {a.detail}
            </Banner>
          ))}
          {error && <Banner kind="error">{error}</Banner>}

          <CaseView
            summary={selected}
            user={user}
            onDisposed={async () => { await load(false); advance() }}
            onSkip={advance}
          />
        </section>
      </div>
    </>
  )
}
