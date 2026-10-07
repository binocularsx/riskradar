import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api, clock, nairaShort } from '../lib/api'
import { useAlertStream } from '../lib/useStream'
import { Banner, RiskBadge } from '../components/ui'
import { CaseIcon, CaseMetric } from '../components/CaseWorkspace'

const SCOPES = [
  { key: 'mine', label: 'My cases' },
  { key: 'all', label: 'All cases' },
  { key: 'unassigned', label: 'Unassigned' },
  { key: 'breaching', label: 'Due soon & overdue' },
  { key: 'escalated', label: 'Escalated' },
  { key: 'awaiting_approval', label: 'Awaiting approval' },
  { key: 'awaiting_close', label: 'Ready to close' },
]
const PAGE_SIZE = 15
const STATE_LABELS = { OPEN: 'Not started', UNDER_REVIEW: 'In review', NEW: 'Not started', IN_REVIEW: 'In review', ESCALATED: 'With a specialist', AWAITING_CLOSE: 'Ready to close', CLOSED: 'Closed' }

export default function CaseQueue({ user }) {
  const canReview = user.permissions.includes('cases:review')
  const supervises = user.permissions.includes('cases:close')
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedScope = searchParams.get('scope')
  const scope = SCOPES.some((s) => s.key === requestedScope) ? requestedScope : supervises ? 'all' : canReview ? 'mine' : 'all'
  const setScope = (value) => setSearchParams({ scope: value })
  const [q, setQ] = useState('')
  const [severity, setSeverity] = useState('all')
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [alarms, setAlarms] = useState([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)
  const requestId = useRef(0)
  const navigate = useNavigate()

  const load = useCallback(async () => {
    const id = ++requestId.current
    try {
      const result = await api.worklist({ scope, limit: 100 })
      if (id === requestId.current) { setData({ ...result, scope }); setError(null) }
    } catch (e) { if (id === requestId.current) setError(e.message) }
  }, [scope])

  useEffect(() => {
    setError(null)
    load()
    const timer = setInterval(load, 20000)
    return () => { requestId.current += 1; clearInterval(timer) }
  }, [load])
  const { connected } = useAlertStream({
    onAlert: load,
    onAlarm: (a) => setAlarms((prev) => [a, ...prev].slice(0, 3)),
    onCaseNews: load,
  })

  const takeNext = async () => {
    setBusy(true); setNotice(null)
    try {
      const { case: next, note } = await api.nextCase()
      if (!next) { setNotice(note || 'No case is available for you to review.'); return }
      navigate(`/cases/${next.id}`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const current = data?.scope === scope ? data : null
  const s = current?.summary
  const items = (current?.items ?? []).filter((c) => {
    const term = q.trim().toLowerCase()
    return (severity === 'all' || c.risk_level === severity)
      && (!term || `${c.customer_name || ''} CASE-${c.id}`.toLowerCase().includes(term))
  })
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE))
  const currentPage = Math.min(page, pages)
  const start = (currentPage - 1) * PAGE_SIZE
  const filtered = q.trim() || severity !== 'all'
  const resetFilters = () => { setQ(''); setSeverity('all'); setPage(1) }

  return (
    <div className="page ops-dashboard cases-workspace">
      <div className="page-head ops-page-head">
        <div>
          <div className="ops-eyebrow">Case management</div>
          <h1>Cases to review</h1>
          <p className="page-sub">A clear view of your queue. Focus on the cases that matter most.</p>
        </div>
        <div className="ops-head-actions">
          <Link className="ops-action" to="/tracker">View all stages <CaseIcon name="arrow" /></Link>
          {canReview && !supervises && <button className="primary ops-action" onClick={takeNext} disabled={busy}>
            <span aria-hidden="true">+</span> {busy ? 'Finding a case…' : 'Take next case'}
          </button>}
        </div>
      </div>

      {[...new Map(alarms.map((a) => [`${a.code}:${a.detail}`, a])).values()].map((a, i) => (
        <Banner key={i} kind="warn"><strong>System alarm — {a.code}.</strong> {a.detail}</Banner>
      ))}
      {error && <Banner kind="error">{error} <button className="ghost" onClick={load}>Retry</button></Banner>}
      {notice && <Banner kind="info">{notice}</Banner>}

      <div className="statrow">
        <CaseMetric label="Open cases" value={s?.open_cases} icon="cases" featured
          note={s ? `${s.mine} assigned to you in this view` : 'Your current queue at a glance'} />
        <CaseMetric label="Value under investigation" value={s ? nairaShort(s.total_exposure_minor) : null} icon="money"
          note="Approved payments · not confirmed loss" />
        <CaseMetric label="Overdue cases" value={s?.breaching} icon="clock" tone={s?.breaching ? 'danger' : ''}
          note={s ? `${s.due_soon ?? 0} more approaching their deadline` : 'Keep time-sensitive cases moving'} />
        <CaseMetric label="Unassigned cases" value={s?.unassigned} icon="person"
          note="Waiting for an investigator in this view" />
      </div>

      <section className="card cases-panel" aria-labelledby="queue-title">
        <div className="cases-panel-head">
          <div><h2 id="queue-title">Investigation queue</h2><p>Prioritised by urgency, exposure and time waiting.</p></div>
          <span className="live" role="status"><span className={`live-dot ${connected ? 'on' : 'off'}`} />
            {connected ? 'Live updates' : 'Reconnecting'}</span>
        </div>
        <div className="case-tabs" aria-label="Case scope">
          {SCOPES.map((sc) => <button key={sc.key} aria-pressed={scope === sc.key}
            className={scope === sc.key ? 'on' : ''}
            onClick={() => { setScope(sc.key); setPage(1) }}>{sc.label}</button>)}
        </div>
        <div className="cases-filterbar">
          <div className="searchbar cases-search">
            <CaseIcon name="search" />
            <input type="search" aria-label="Search case or customer" value={q}
              onChange={(e) => { setQ(e.target.value); setPage(1) }} placeholder="Search case ID or customer…" />
          </div>
          <div className="cases-filter-actions">
            {filtered && <button className="ghost" onClick={resetFilters}>Clear filters</button>}
            <select aria-label="Filter by severity" value={severity}
              onChange={(e) => { setSeverity(e.target.value); setPage(1) }}>
              <option value="all">All severities</option>
              {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((level) => <option key={level} value={level}>{level[0] + level.slice(1).toLowerCase()}</option>)}
            </select>
          </div>
        </div>

        <div className="table-scroll" role="region" aria-label="Scrollable case queue" tabIndex={0} aria-busy={!current && !error}>
          <table className="rowtable cases-table" aria-label="Cases to review">
            <thead><tr>
              <th scope="col">Case / Customer</th><th scope="col">Severity</th><th scope="col" className="num">Exposure</th>
              <th scope="col">Suggested action</th><th scope="col">Time remaining</th><th scope="col">Assigned to</th><th scope="col"><span className="case-sr-only">Open case</span></th>
            </tr></thead>
            <tbody>
              {items.slice(start, start + PAGE_SIZE).map((c) => (
                <tr key={c.id}>
                  <td><div className="case-customer">
                    <span className="case-customer-icon"><CaseIcon name="cases" /></span>
                    <div><Link className="case-name" to={`/cases/${c.id}`}>{c.customer_name || 'Unknown customer'}</Link>
                      <div className="case-cell-sub">CASE-{c.id}<span>·</span>{STATE_LABELS[c.state] || 'Open'}
                        <span>·</span>{c.alert_count} alert{c.alert_count === 1 ? '' : 's'}</div>
                      {(c.handling === 'MACHINE' || c.watchlisted || c.industry_flagged) && <div className="cellflags">
                        {c.handling === 'MACHINE' && <span className="pill suppress">System handled</span>}
                        {c.watchlisted && <span className="pill escalate">Flagged</span>}
                        {c.industry_flagged && <span className="pill override">Flagged elsewhere</span>}
                      </div>}
                    </div>
                  </div></td>
                  <td><RiskBadge level={c.risk_level} /></td>
                  <td className="num case-amount">{nairaShort(c.exposure_minor)}</td>
                  <td className="case-recommendation">{c.recommendation?.action || '—'}</td>
                  <td><span className={`sla sla-${c.sla_state}`}>{clock(c.sla_remaining_minutes)}</span>
                    {c.sla_state === 'BREACHED' && <div className="case-cell-sub">Overdue</div>}</td>
                  <td><span className={`case-assignee ${c.assignee_id === user.id ? 'is-mine' : ''}`}>
                    <CaseIcon name="person" />{c.assignee_id == null ? 'Unassigned' : c.assignee_id === user.id ? 'You' : c.assignee_name || 'Assigned'}
                  </span></td>
                  <td><Link className="case-open" to={`/cases/${c.id}`} aria-label={`Open case ${c.id}`}><CaseIcon name="arrow" /></Link></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!items.length && <div className="cases-empty" role="status">
            <span className="cases-empty-icon"><CaseIcon name={filtered ? 'search' : 'cases'} /></span>
            <strong>{!current ? (error ? 'Unable to load cases' : 'Loading your queue…') : filtered ? 'No matching cases' : 'You’re all caught up'}</strong>
            <p>{!current ? (error ? 'Use Retry above to reconnect.' : 'Getting the latest cases and priorities.') : filtered ? 'Try a different name, case ID or severity.' : 'There are no cases in this view. Check another stage or the shared queue.'}</p>
            {current && filtered && <button className="ops-action" onClick={resetFilters}>Clear filters</button>}
          </div>}
        </div>
        <div className="cases-table-foot">
          <span>{current ? (items.length ? `Showing ${start + 1}–${Math.min(start + PAGE_SIZE, items.length)} of ${items.length} cases` : '0 cases') : 'Waiting for cases'}
            {current?.total > current?.items.length && <span className="case-limit-note"> · First {current.items.length} of {current.total} prioritised cases loaded</span>}</span>
          <div className="cases-pagination" aria-label="Queue pagination">
            <button aria-label="Previous page" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>‹</button>
            <span>Page {currentPage} of {pages}</span>
            <button aria-label="Next page" disabled={currentPage === pages} onClick={() => setPage(currentPage + 1)}>›</button>
          </div>
        </div>
      </section>
      <p className="cases-footnote"><CaseIcon name="clock" /> Queue totals reflect the selected view. Highest-priority cases appear first.</p>
    </div>
  )
}
