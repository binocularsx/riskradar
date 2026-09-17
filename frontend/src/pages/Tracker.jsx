import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'

/**
 * The case tracker (D83): every open case in the stage it is in, and how long
 * it has waited there, so nothing stalls unseen between an analyst, InfoSec
 * and the lead who closes it.
 *
 * NEW → IN REVIEW → (ESCALATED) → AWAITING CLOSE → CLOSED. The oldest case in
 * each column is at the top: that is the one to chase.
 */

const STAGE_HELP = {
  NEW: 'Raised by an alert; nobody has taken it. Start reviewing in Triage hands out the most urgent.',
  IN_REVIEW: 'An analyst is working the steps. It leaves this column when they record an outcome or escalate.',
  ESCALATED: 'Waiting for InfoSec or the Fraud Ops lead to take it. They hand it back with findings or close it.',
  AWAITING_CLOSE: 'The outcome is recorded. A Fraud Ops lead checks it and closes the case.',
  CLOSED: 'Closed in the last 24 hours. Confirmed fraud added its destinations to the known-mule list.',
}
const OUTCOME = { CONFIRMED_FRAUD: 'fraud', FALSE_POSITIVE: 'false alarm', INCONCLUSIVE: 'inconclusive' }

function age(minutes) {
  if (minutes == null) return '—'
  if (minutes < 60) return `${Math.round(minutes)}m`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`
  return `${Math.floor(minutes / 1440)}d`
}

export default function Tracker() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () => api.pipeline().then((d) => alive && setData(d)).catch((e) => alive && setError(e.message))
    load()
    const timer = setInterval(load, 10000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  if (!data) return error ? <Banner kind="error">{error}</Banner> : <p className="dim">Loading the tracker…</p>

  return (
    <div>
      {error && <Banner kind="error">{error}</Banner>}
      <p className="muted" style={{ marginTop: 0, maxWidth: 760 }}>
        Every open case, in the stage it is in, oldest first, with how long it has waited in that stage.
        Click a case to open it in Triage. {data.window}.
      </p>
      <div className="tracker">
        {data.stages.map((s) => (
          <section className="tracker-col" key={s.key} id={s.key}>
            <header>
              <div className="between">
                <strong>{s.label}</strong>
                <span className="tracker-count">{s.count}</span>
              </div>
              <div className="dim" style={{ fontSize: 11.5, marginTop: 4 }}>{STAGE_HELP[s.key]}</div>
              <div className="row wrap" style={{ marginTop: 8, gap: 6 }}>
                {s.exposure_minor > 0 && <span className="tag">{nairaShort(s.exposure_minor)} at risk</span>}
                {s.oldest_minutes != null && s.count > 0 && <span className="tag">oldest {age(s.oldest_minutes)}</span>}
                {s.by_team && Object.entries(s.by_team).filter(([, n]) => n).map(([t, n]) => (
                  <span className="pill" key={t}>{n} with {t === 'INFOSEC' ? 'InfoSec' : 'Fraud Ops'}</span>))}
                {s.by_outcome && Object.entries(s.by_outcome).filter(([, n]) => n).map(([o, n]) => (
                  <span className="pill" key={o}>{n} {OUTCOME[o]}</span>))}
              </div>
            </header>
            <div className="tracker-items">
              {s.items.map((c) => (
                <Link to={`/triage?case=${c.id}`} className="tracker-card" key={c.id}>
                  <div className="between">
                    <span className="mono dim">#{c.id}</span>
                    <RiskBadge level={c.risk_level} />
                  </div>
                  <div style={{ margin: '6px 0 4px' }}>{c.customer_name || 'Unknown customer'}</div>
                  <div className="between dim" style={{ fontSize: 11.5 }}>
                    <span>{nairaShort(c.exposure_minor)}</span>
                    <span>{s.key === 'CLOSED' ? `closed ${age(c.minutes_in_stage)} ago` : `${age(c.minutes_in_stage)} here`}</span>
                  </div>
                  <div className="dim" style={{ fontSize: 11.5, marginTop: 4 }}>
                    {c.assignee_name ? `with ${c.assignee_name}` : c.escalated_to ? `waiting for ${c.escalated_to === 'INFOSEC' ? 'InfoSec' : 'Fraud Ops'}` : 'nobody yet'}
                    {c.actions_recorded ? ` · ${c.actions_recorded} step${c.actions_recorded === 1 ? '' : 's'} recorded` : ''}
                    {c.outcome ? ` · ${OUTCOME[c.outcome]}` : ''}
                  </div>
                </Link>
              ))}
              {!s.items.length && <div className="dim" style={{ fontSize: 12, padding: 10 }}>Nothing here.</div>}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}
