import { Link } from 'react-router-dom'
import { api, clock, nairaShort, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'
import { RiskBadge, SegmentedProgress } from '../components/ui'
import { CaseMetric } from '../components/CaseWorkspace'
import ReadStatus from '../components/ReadStatus'
import ServiceHealth, { adapterDescription } from '../components/ServiceHealth'

const LEVELS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const AGE_ORDER = ['under 30m', '30m - 2h', '2h - 8h', '8h - 24h', 'over 24h']
const OUTCOME = { CONFIRMED_FRAUD: 'Fraud confirmed', FALSE_POSITIVE: 'No fraud found', INCONCLUSIVE: 'Inconclusive' }
const loadWork = () => api.worklist({ scope: 'all', limit: 6 })
const duration = (minutes) => minutes == null ? '—' : minutes >= 60 ? `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m` : `${Math.round(minutes)}m`

function Metric({ to, ...props }) {
  return to ? <Link className="ops-metric-link" to={to}><CaseMetric {...props} /></Link> : <CaseMetric {...props} />
}

export default function Operations({ user }) {
  const canRead = user.permissions.includes('cases:read')
  const canApprove = user.permissions.includes('cases:approve_fraud')
  const canSeeAll = user.permissions.includes('cases:close')
  const operations = usePolling(api.operations)
  const system = usePolling(api.systemStatus)
  const integrations = usePolling(api.integrations, 60000)
  const work = usePolling(loadWork, 20000, canRead)
  const approvals = usePolling(api.approvals, 20000, canApprove)
  const restrictions = usePolling(api.restrictionStatus, 20000, canRead)
  const reconciliation = usePolling(api.restrictionReconciliation, 20000, canSeeAll)
  const resources = [operations, system, integrations, ...(canRead ? [work, restrictions] : []), ...(canApprove ? [approvals] : []), ...(canSeeAll ? [reconciliation] : [])]
  const data = operations.data
  const backlog = LEVELS.map((level) => data?.backlog.find((b) => b.risk_level === level) || { risk_level: level, cases: 0, exposure_minor: 0 })
  const total = backlog.reduce((n, b) => n + Number(b.cases), 0)
  const exposure = backlog.reduce((n, b) => n + Number(b.exposure_minor), 0)
  const outcomes = data?.outcomes || []
  // `decided` is at least 1 on the API, even when no case has an outcome.
  const decided = outcomes.reduce((n, o) => n + Number(o.n), 0)
  const s = work.data?.summary
  const waitingApprovals = approvals.data?.summary
  const budget = system.data?.budget
  const r = restrictions.data
  const rec = reconciliation.data
  const exceptions = rec ? [
    ...rec.delivered_not_acknowledged.map((x) => ({ ...x, issue: `No bank response after ${duration(x.waiting_seconds / 60)}` })),
    ...rec.not_delivered.map((x) => ({ ...x, issue: x.last_error || 'Not yet delivered to the bank' })),
    ...rec.expired_still_standing.map((x) => ({ ...x, issue: 'Temporary restriction expired; no release order recorded' })),
  ] : []

  return <div className="page ops-dashboard operations-workspace">
    <header className="page-head ops-page-head">
      <div><div className="ops-eyebrow">Operational oversight</div><h1>Operations</h1>
        <p className="page-sub">Service readiness, work needing attention and investigation outcomes.</p></div>
      <div className="ops-head-actions">
        <button className="ops-action" disabled={resources.some((x) => x.loading)} onClick={() => resources.forEach((x) => x.refresh())}>Refresh overview</button>
        <Link className="ops-action" to="/analytics">Performance reports</Link>
        {canRead && <Link className="ops-action primary" to="/triage?scope=all">Review cases</Link>}
      </div>
    </header>
    <ServiceHealth resource={system} integrations={integrations} />
    <ReadStatus resource={operations} label="Operations overview" />
    <div className="ops-section-heading"><h2>Work requiring attention</h2><span className="dim">Open-case totals cover the bank. Case links follow your access.</span></div>
    <div className="statrow">
      <Metric label="Open cases" value={data ? total : null} note="All open investigation states" icon="cases" featured to={canRead ? '/tracker' : null} />
      <Metric label="Value under investigation" value={data ? nairaShort(exposure) : null} note="Approved payments in open cases · not confirmed loss" icon="money" to={canRead ? '/triage?scope=all' : null} />
      {canRead ? <Metric label="Overdue cases" value={s?.breaching} note={s ? `${s.due_soon} more due soon · within your access` : 'Case deadlines unavailable until loaded'} icon="clock" tone={s?.breaching ? 'danger' : ''} to="/triage?scope=breaching" />
        : <Metric label="Critical cases" value={data ? Number(backlog[0].cases) : null} note="Aggregate only · case access restricted" icon="clock" />}
      {canApprove ? <Metric label="Pending approvals" value={s?.awaiting_approval} note={waitingApprovals ? `${waitingApprovals.mine_awaiting_someone_else} of your proposals await another lead` : 'Includes proposals awaiting a different lead'} icon="check" to="/approvals" />
        : <Metric label="Closed in last 7 days" value={data?.resolution?.closed_7d} note={data ? `Median resolution ${duration(data.resolution?.median_minutes)}` : 'Resolution history loading'} icon="check" />}
    </div>
    {canApprove && <ReadStatus resource={approvals} label="Approvals" />}
    {canRead && <section className="card" aria-labelledby="priority-title">
      <div className="between wrap"><h2 id="priority-title">Priority work</h2><Link to="/triage?scope=all">Open review queue →</Link></div>
      <ReadStatus resource={work} label="Priority work" />
      {s && <div className="row wrap ops-attention-links">
        <Link to="/triage?scope=breaching">{s.breaching} overdue · {s.due_soon} due soon</Link>
        <Link to="/triage?scope=awaiting_approval">{s.awaiting_approval} awaiting approval</Link>
        <Link to="/triage?scope=awaiting_close">{s.awaiting_close} awaiting closure</Link>
        <span>{s.regulatory_breached} cases with a breached regulatory clock</span>
      </div>}
      {work.data && <div className="table-scroll"><table className="rowtable">
        <thead><tr><th>Case / customer</th><th>Risk</th><th>Owner</th><th>Review deadline</th><th>Next step</th></tr></thead>
        <tbody>{work.data.items.map((c) => <tr key={c.id}>
          <td><Link to={`/cases/${c.id}`}>CASE-{c.id}</Link><div className="dim">{c.customer_name}</div></td>
          <td><RiskBadge level={c.risk_level} /></td><td>{c.assignee_name || 'Unassigned'}</td>
          <td><span className={`sla sla-${c.sla_state}`}>{c.sla_remaining_minutes == null ? 'Unavailable' : clock(c.sla_remaining_minutes)}</span></td>
          <td>{c.awaiting_approval ? 'A different lead reviews the proposal' : c.outcome ? 'Review closure requirements' : c.recommendation?.action || 'Review evidence'}</td>
        </tr>)}{!work.data.items.length && <tr><td colSpan={5}>No open cases within your access.</td></tr>}</tbody>
      </table><p className="dim">Top {work.data.items.length} of {work.data.total} visible open cases, ordered by priority.</p></div>}
    </section>}
    {canRead && <section className="card" id="bank-actions" aria-labelledby="bank-actions-title">
      <h2 id="bank-actions-title">Bank action delivery</h2>
      <p className="dim">Approval authorises a request. Delivery does not mean the bank applied it. Counts cover recorded orders across the bank.</p>
      <ReadStatus resource={restrictions} label="Bank action delivery" />
      {r && <>
        <p className={['none', 'loopback'].includes(r.connector) ? 'service-problems' : 'dim'}>{adapterDescription(r.connector)}</p>
        <div className="service-facts"><span><strong>{r.lifecycle.awaiting_delivery}</strong> awaiting delivery</span><span><strong>{r.lifecycle.awaiting_ack}</strong> delivered, awaiting bank outcome</span><span><strong>{r.lifecycle.acknowledged}</strong> bank outcomes received</span></div>
        <div className="row wrap">{r.by_outcome.map((o) => <span key={o.outcome} className={`pill ${o.outcome === 'APPLIED' ? 'suppress' : 'escalate'}`}>{o.outcome === 'APPLIED' ? 'Bank reports applied' : o.outcome === 'NOT_APPLIED' ? 'Bank reports not applied' : 'Bank rejected'}: {o.n}</span>)}</div>
        {r.outbox.filter((o) => o.status !== 'SENT' && o.last_error).map((o) => <p className="service-problems" key={o.status}>{o.n} {o.status.toLowerCase()}: {o.last_error}</p>)}
      </>}
      {canSeeAll && <><ReadStatus resource={reconciliation} label="Delivery exceptions" />
        {rec && <details className="ops-detail" open={exceptions.length > 0}>
          <summary>{exceptions.length} delivery exceptions · response target {rec.ack_overdue_hours}h</summary>
          {exceptions.length ? <ul className="ops-exceptions">{exceptions.slice(0, 10).map((x, i) => <li key={`${x.restriction_ref}:${i}`}><Link to={`/cases/${x.case_id}`}>CASE-{x.case_id}</Link> · {x.issue}<small className="mono dim">Request {x.restriction_ref}</small></li>)}</ul> : <p className="dim">No exceptions reported at {when(rec.checked_at)}.</p>}
          {exceptions.length > 10 && <p className="dim">Showing the first 10 exceptions.</p>}
        </details>}</>}
    </section>}
    <div className="ops-section-heading"><h2>Team workload</h2><span className="dim">Current open cases · closures in the last 24 hours</span></div>
    {data && <>
      <div className="grid ops-primary-grid">
        <section className="card"><h3>Open cases by risk</h3>{backlog.map((b) => <div className="ops-risk-row" key={b.risk_level}>
          <div className="between"><RiskBadge level={b.risk_level} /><strong>{b.cases} cases</strong></div>
          <SegmentedProgress value={total ? Number(b.cases) / total * 100 : 0} label={`${b.risk_level.toLowerCase()} share of open cases`} color={`var(--${b.risk_level.toLowerCase()})`} />
          <p className="dim">Average age {Number(b.cases) ? duration(b.avg_age_minutes) : '—'} · review target {duration(data.sla_minutes[b.risk_level])}</p>
        </div>)}</section>
        <section className="card"><h3>Case ageing</h3><p className="dim">Age since opening, not time actively investigated.</p>{AGE_ORDER.map((bucket) => <div className="between ops-age-row" key={bucket}><span>{bucket}</span><strong>{data.ageing.find((a) => a.bucket === bucket)?.cases ?? 0}</strong></div>)}
          <p className="dim">Median time to close: {duration(data.resolution?.median_minutes)} across {data.resolution?.closed_7d ?? 0} cases closed in the last 7 days.</p>
        </section>
      </div>
      <section className="card"><h3>Assigned workload</h3><p className="dim">Assignment counts do not indicate whether a team member is online or at capacity.</p><div className="table-scroll"><table className="rowtable">
        <thead><tr><th>Team member</th><th>Role</th><th className="num">Open cases</th><th className="num">Closed · last 24h</th></tr></thead>
        <tbody>{data.analysts.map((a, i) => <tr key={`${a.display_name}:${a.role}:${i}`}><td>{a.display_name}</td><td>{a.role.replace(/_/g, ' ').toLowerCase()}</td><td className="num">{a.open_cases}</td><td className="num">{a.closed_24h}</td></tr>)}{!data.analysts.length && <tr><td colSpan={4}>No team workload returned.</td></tr>}</tbody>
      </table></div></section>
      <div className="ops-section-heading"><h2>Investigation results</h2><span className="dim">All recorded outcomes · includes cases awaiting closure</span></div>
      <section className="card"><h3>{decided} cases with an outcome</h3>
        <div className="service-facts">{outcomes.map((o) => <span key={o.outcome}><strong>{o.n}</strong> {OUTCOME[o.outcome] || o.outcome} · {decided ? Math.round(Number(o.n) / decided * 100) : 0}%</span>)}</div>
        {!decided && <p className="dim">No investigation outcomes recorded yet.</p>}
        <p className="dim">These are investigation findings, not a measure of fraud prevented or money recovered.</p>
      </section>
    </>}
    {budget && <section className="card"><h2>Daily review limit · {budget.local_day} (Nigeria)</h2>
      <div className="service-facts"><span><strong>{budget.today.raised} / {budget.config.per_day}</strong> alerts raised / daily limit</span><span><strong>{budget.waiting.count}</strong> held-back alerts waiting</span><span><strong>{budget.waiting.critical}</strong> critical alerts held back</span><span><strong>{budget.today.expired}</strong> held-back alerts expired today</span></div>
      <p className="dim">The daily limit is {budget.config.enforced ? 'on' : 'off'}. It controls how many alerts reach the team each day; it does not measure how many people are working. Held-back alerts still need someone to keep an eye on them.</p>
    </section>}
    <p className="dim ops-refresh-note">Operational reads refresh every 20 seconds; identity connections every minute. {operations.updatedAt && <>Overview last read {when(operations.updatedAt)}.</>}</p>
  </div>
}
