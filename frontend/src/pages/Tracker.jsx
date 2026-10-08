import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'
import { CaseIcon, CaseMetric } from '../components/CaseWorkspace'

/**
 * The case tracker (D83): every open case in the stage it is in, and how long
 * it has waited there, so nothing stalls unseen between an analyst and the
 * lead who closes it.
 *
 * NEW → IN REVIEW → (ESCALATED) → AWAITING CLOSE → CLOSED. The oldest case in
 * each column is at the top: that is the one to chase.
 */

const STAGE_HELP = {
  NEW: 'Nobody has started these cases yet. The oldest case appears first.',
  IN_REVIEW: 'A team member is currently checking these cases.',
  ESCALATED: 'These cases were escalated for specialist review.',
  AWAITING_APPROVAL: 'A team lead needs to check the proposed investigation result.',
  AWAITING_CLOSE: 'The investigation is complete and waiting for a final check.',
  CLOSED: 'These cases were completed in the last 24 hours.',
}
const STAGE_LABEL = { NEW: 'Not started', IN_REVIEW: 'Being reviewed', ESCALATED: 'With a specialist', AWAITING_APPROVAL: 'Awaiting approval', AWAITING_CLOSE: 'Ready to close', CLOSED: 'Completed' }
const OUTCOME = { CONFIRMED_FRAUD: 'fraud confirmed', FALSE_POSITIVE: 'no fraud found', INCONCLUSIVE: 'more review needed' }

function age(minutes) {
  if (minutes == null) return '—'
  if (minutes < 60) return `${Math.round(minutes)}m`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`
  return `${Math.floor(minutes / 1440)}d`
}

export default function Tracker() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    let alive = true
    const load = () => api.pipeline().then((d) => {
      if (alive) { setData(d); setError(null) }
    }).catch((e) => alive && setError(e.message))
    load()
    const timer = setInterval(load, 10000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  const stages = data?.stages || []
  const openStages = stages.filter((s) => s.key !== 'CLOSED')
  const totalOpen = openStages.reduce((sum, s) => sum + Number(s.count), 0)
  const exposure = openStages.reduce((sum, s) => sum + Number(s.exposure_minor || 0), 0)
  const count = (key) => stages.find((s) => s.key === key)?.count ?? 0
  const term = q.trim().toLowerCase()

  return (
    <div className="page ops-dashboard cases-workspace cases-tracker">
      <div className="page-head ops-page-head">
        <div><div className="ops-eyebrow">Case management</div><h1>Progress board</h1>
          <p className="page-sub">Where every investigation stands, from first review to final resolution.</p></div>
        <Link className="ops-action" to="/triage">Review queue <CaseIcon name="arrow" /></Link>
      </div>
      {error && <Banner kind="error">{error}</Banner>}
      <div className="statrow">
        <CaseMetric label="Open cases" value={data ? totalOpen : null} note="Across stages within your access" icon="cases" featured />
        <CaseMetric label="Value under investigation" value={data ? nairaShort(exposure) : null} note="Approved payments in visible open cases" icon="money" />
        <CaseMetric label="Ready to close" value={data ? count('AWAITING_CLOSE') : null} note="Waiting for a final review" icon="clock" />
        <CaseMetric label="Completed" value={data ? count('CLOSED') : null} note="Cases closed in the last 24 hours" icon="check" />
      </div>
      <section className="card cases-panel" aria-labelledby="stages-title">
        <div className="cases-panel-head">
          <div><h2 id="stages-title">Investigation stages</h2><p>Follow each case. See where attention is needed.</p></div>
          <span className="cases-refresh-note"><CaseIcon name="clock" /> Updates every 10s</span>
        </div>
        <div className="cases-filterbar">
          <div className="searchbar cases-search"><CaseIcon name="search" />
            <input type="search" aria-label="Search tracked cases" placeholder="Search case ID or customer…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <span className="cases-refresh-note">Oldest open cases first · Scroll to explore stages <CaseIcon name="arrow" /></span>
        </div>
      <div className="tracker" aria-label="Cases by investigation stage" tabIndex={0}>
        {stages.map((s) => (
          <section className={`tracker-col stage-${s.key.toLowerCase()}`} key={s.key} id={s.key}>
            <header>
              <div className="between">
                <strong className="tracker-stage-name"><span className="tracker-stage-dot" />{STAGE_LABEL[s.key] || s.label}</strong>
                <span className="tracker-count">{s.count}</span>
              </div>
              <p className="tracker-stage-help">{STAGE_HELP[s.key]}</p>
              <div className="row wrap" style={{ marginTop: 8, gap: 6 }}>
                {s.exposure_minor > 0 && <span className="tag">{nairaShort(s.exposure_minor)} under review</span>}
                {s.oldest_minutes != null && s.count > 0 && <span className="tag">oldest {age(s.oldest_minutes)}</span>}
                {s.by_team && Object.entries(s.by_team).filter(([, n]) => n).map(([t, n]) => (
                  <span className="pill" key={t}>{n} with the Fraud team</span>))}
                {s.by_outcome && Object.entries(s.by_outcome).filter(([, n]) => n).map(([o, n]) => (
                  <span className="pill" key={o}>{n} {OUTCOME[o]}</span>))}
              </div>
            </header>
            <div className="tracker-items">
              {s.items.filter((c) => !term || `${c.customer_name || ''} CASE-${c.id}`.toLowerCase().includes(term)).map((c) => (
                <Link to={`/cases/${c.id}`} className="tracker-card" key={c.id}>
                  <div className="between">
                    <span className="case-cell-sub">CASE-{c.id}</span>
                    <RiskBadge level={c.risk_level} />
                  </div>
                  <div className="tracker-customer-name">{c.customer_name || 'Unknown customer'}</div>
                  <div className="between dim" style={{ fontSize: 11.5 }}>
                    <span>{nairaShort(c.exposure_minor)}</span>
                    <span>{s.key === 'CLOSED' ? `closed ${age(c.minutes_in_stage)} ago` : `${age(c.minutes_in_stage)} here`}</span>
                  </div>
                  <div className="dim" style={{ fontSize: 11.5, marginTop: 4 }}>
                    {c.assignee_name ? `with ${c.assignee_name}` : c.escalated_to ? 'waiting for the Fraud team' : 'not assigned yet'}
                    {c.actions_recorded ? ` · ${c.actions_recorded} step${c.actions_recorded === 1 ? '' : 's'} recorded` : ''}
                    {c.outcome ? ` · ${OUTCOME[c.outcome]}` : ''}
                  </div>
                </Link>
              ))}
              {!s.items.some((c) => !term || `${c.customer_name || ''} CASE-${c.id}`.toLowerCase().includes(term)) &&
                <div className="tracker-empty"><CaseIcon name={term ? 'search' : 'cases'} />{term ? 'No matching cases' : 'No cases in this stage'}</div>}
            </div>
          </section>
        ))}
      </div>
      {!data && <div className="cases-empty" role="status"><CaseIcon name="cases" /><strong>{error ? 'Unable to load stages' : 'Loading investigations…'}</strong><p>{error ? 'We’ll try again automatically.' : 'Getting the latest case progress.'}</p></div>}
      </section>
      <p className="cases-footnote"><CaseIcon name="clock" />{data?.window || 'Open investigations and cases completed in the last 24 hours.'} Stage totals include all cases, regardless of search.</p>
    </div>
  )
}
