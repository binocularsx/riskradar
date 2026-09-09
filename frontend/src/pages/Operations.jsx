import { useEffect, useState } from 'react'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'

/**
 * The Fraud Ops Lead's view — and the one to put in front of a stakeholder.
 *
 * It deliberately does not answer "how good is the model". That question
 * belongs in the QA report, is answered with held-out evaluation, and changes
 * once a quarter. This screen answers the question a lead has every morning:
 * **is the desk coping?**
 *
 * Four things say whether it is: how much is waiting, how old the oldest work
 * is, who is carrying what, and how much of what we raised turned out to be
 * nothing. The last one is the honest measure of whether the thresholds are set
 * right — a desk with a 95% false-positive rate is being drowned regardless of
 * how good the detection is.
 */

const LEVELS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const AGE_ORDER = ['under 30m', '30m - 2h', '2h - 8h', '8h - 24h', 'over 24h']

export default function Operations() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const fetch = () =>
      api.operations()
        .then((d) => !cancelled && setData(d))
        .catch((e) => !cancelled && setError(e.message))
    fetch()
    const timer = setInterval(fetch, 20000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  const backlog = LEVELS.map(
    (l) => data.backlog.find((b) => b.risk_level === l)
      || { risk_level: l, cases: 0, exposure_minor: 0, avg_age_minutes: 0 }
  )
  const totalCases = backlog.reduce((a, b) => a + Number(b.cases), 0)
  const totalExposure = backlog.reduce((a, b) => a + Number(b.exposure_minor), 0)

  const ageing = AGE_ORDER.map(
    (b) => data.ageing.find((a) => a.bucket === b) || { bucket: b, cases: 0 }
  )
  const maxAge = Math.max(...ageing.map((a) => Number(a.cases)), 1)

  const outcomes = data.outcomes || []
  const decided = data.decided || 1
  const fpRate = data.false_positive_rate

  return (
    <>
      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card">
          <h3>Open cases</h3>
          <div style={{ fontSize: 30, fontWeight: 640, letterSpacing: '-.02em' }}>{totalCases}</div>
          <div className="dim" style={{ fontSize: 12 }}>across all severities</div>
        </div>
        <div className="card">
          <h3>Money at risk</h3>
          <div style={{ fontSize: 30, fontWeight: 640, letterSpacing: '-.02em' }}>
            {nairaShort(totalExposure)}
          </div>
          <div className="dim" style={{ fontSize: 12 }}>approved value on open cases</div>
        </div>
        <div className="card">
          <h3>False positive rate</h3>
          <div style={{ fontSize: 30, fontWeight: 640, letterSpacing: '-.02em',
                        color: fpRate > 0.85 ? 'var(--warn)' : 'var(--text)' }}>
            {Math.round(fpRate * 100)}%
          </div>
          <div className="dim" style={{ fontSize: 12 }}>of {decided} decided cases</div>
        </div>
        <div className="card">
          <h3>Oldest work</h3>
          <div style={{ fontSize: 30, fontWeight: 640, letterSpacing: '-.02em' }}>
            {ageing.filter((a) => Number(a.cases) > 0).slice(-1)[0]?.bucket ?? '—'}
          </div>
          <div className="dim" style={{ fontSize: 12 }}>the bucket still holding cases</div>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>Backlog by severity</h2>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Severity</th><th className="num">Cases</th><th className="num">At risk</th>
                    <th className="num">Avg age</th><th className="num">Target</th></tr>
              </thead>
              <tbody>
                {backlog.map((b) => {
                  const target = data.sla_minutes[b.risk_level]
                  const over = Number(b.avg_age_minutes) > target
                  return (
                    <tr key={b.risk_level}>
                      <td><RiskBadge level={b.risk_level} /></td>
                      <td className="num">{b.cases}</td>
                      <td className="num">{nairaShort(b.exposure_minor)}</td>
                      <td className="num" style={{ color: over ? 'var(--danger)' : 'inherit' }}>
                        {Number(b.avg_age_minutes) >= 60
                          ? `${Math.round(Number(b.avg_age_minutes) / 60)}h`
                          : `${Number(b.avg_age_minutes) || 0}m`}
                      </td>
                      <td className="num dim">
                        {target >= 60 ? `${target / 60}h` : `${target}m`}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="dim" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
            Average age above target means that severity is not being reached in
            time. Critical work ageing past fifteen minutes is the number that
            should start a conversation.
          </p>
        </div>

        <div className="card">
          <h2>How long cases have been waiting</h2>
          {ageing.map((a) => (
            <div key={a.bucket} style={{ marginBottom: 12 }}>
              <div className="between" style={{ fontSize: 12.5, marginBottom: 4 }}>
                <span className="muted">{a.bucket}</span>
                <span className="mono">{a.cases}</span>
              </div>
              <div className="bar-track">
                <div className="bar-fill"
                     style={{
                       width: `${(Number(a.cases) / maxAge) * 100}%`,
                       background: a.bucket === 'over 24h' || a.bucket === '8h - 24h'
                         ? 'var(--danger)' : 'var(--accent)',
                     }} />
              </div>
            </div>
          ))}
          <p className="dim" style={{ fontSize: 11.5, marginBottom: 0 }}>
            A healthy desk is heavily weighted to the left. Weight on the right
            means work is being started and abandoned, or the queue is bigger
            than the team.
          </p>
        </div>

        <div className="card">
          <h2>Who is carrying what</h2>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Analyst</th><th>Role</th><th className="num">Open</th><th className="num">Closed 24h</th></tr>
              </thead>
              <tbody>
                {data.analysts.map((a) => (
                  <tr key={a.display_name}>
                    <td>{a.display_name}</td>
                    <td className="muted">{a.role.replace(/_/g, ' ').toLowerCase()}</td>
                    <td className="num">{a.open_cases}</td>
                    <td className="num">{a.closed_24h}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="dim" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
            The alert budget assumes three analysts at forty reviewable alerts
            each. If "open" climbs well past that, the thresholds need
            re-deriving — not the team working harder.
          </p>
        </div>

        <div className="card">
          <h2>What the alerts turned out to be</h2>
          {outcomes.map((o) => {
            const pct = Math.round((Number(o.n) / decided) * 100)
            const colour = o.outcome === 'CONFIRMED_FRAUD' ? 'var(--critical)'
              : o.outcome === 'FALSE_POSITIVE' ? 'var(--low)' : 'var(--medium)'
            return (
              <div key={o.outcome} style={{ marginBottom: 12 }}>
                <div className="between" style={{ fontSize: 12.5, marginBottom: 4 }}>
                  <span>{o.outcome.replace(/_/g, ' ').toLowerCase()}</span>
                  <span className="mono">{o.n} · {pct}%</span>
                </div>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${pct}%`, background: colour }} />
                </div>
              </div>
            )
          })}
          {!outcomes.length && <p className="dim">No cases decided yet.</p>}
          <p className="dim" style={{ fontSize: 11.5, marginBottom: 0 }}>
            These are not a status field. Confirmed fraud and false positive are
            analyst-labelled ground truth — the only non-circular labels this
            system will ever produce, and what a future model would be retrained
            against.
          </p>
        </div>
      </div>
    </>
  )
}
