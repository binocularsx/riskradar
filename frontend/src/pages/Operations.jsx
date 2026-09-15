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

/**
 * Alerts raised each hour against what the team can review in an hour. The
 * dashed line is the budget every threshold was derived from (three analysts
 * at twenty-five a day, D76); bars above it turn orange and say so in the tooltip, so
 * the overrun never relies on colour alone.
 */
function AlertsVsCapacity({ overview }) {
  const [tip, setTip] = useState(null)
  const perHour = overview.alert_budget.per_day / 24
  const byHour = {}
  for (const r of overview.alert_volume) {
    byHour[r.bucket] = (byHour[r.bucket] || 0) + Number(r.alerts)
  }
  const hours = Object.keys(byHour).sort().map((b) => ({ at: b, n: byHour[b] }))
  if (!hours.length) return <p className="dim">No alerts in the last day.</p>

  const W = 760, H = 230, P = { l: 38, r: 14, t: 16, b: 30 }
  const max = Math.max(perHour * 1.4, ...hours.map((h) => h.n))
  const bw = (W - P.l - P.r) / hours.length
  const y = (v) => H - P.b - (v / max) * (H - P.t - P.b)
  const label = (at) => new Date(at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  const total = hours.reduce((a, h) => a + h.n, 0)

  return (
    <div className="chart" onMouseLeave={() => setTip(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`${total} alerts in the last day against a capacity of ${Math.round(perHour * 24)}`}>
        {[0, Math.round(max / 2), Math.round(max)].map((v) => (
          <g key={v}>
            <line x1={P.l} x2={W - P.r} y1={y(v)} y2={y(v)} stroke="var(--line)" />
            <text x={P.l - 8} y={y(v) + 4} textAnchor="end" fontSize="10" fill="var(--text-3)"
                  fontFamily="var(--mono)">{v}</text>
          </g>
        ))}
        {hours.map((h, i) => {
          const over = h.n > perHour
          return (
            <g key={h.at}>
              <rect x={P.l + i * bw + 2} y={y(h.n)} width={Math.max(bw - 4, 1)}
                    height={Math.max(H - P.b - y(h.n), 0)} rx="4"
                    fill={over ? 'var(--high)' : 'var(--accent)'} />
              <rect x={P.l + i * bw} y={P.t} width={bw} height={H - P.t - P.b} fill="transparent"
                    onMouseMove={(e) => {
                      const box = e.currentTarget.ownerSVGElement.parentNode.getBoundingClientRect()
                      setTip({ x: e.clientX - box.left, y: e.clientY - box.top, h, over })
                    }} />
              {i % 3 === 0 && (
                <text x={P.l + i * bw + bw / 2} y={H - 10} textAnchor="middle" fontSize="10"
                      fill="var(--text-3)" fontFamily="var(--mono)">{label(h.at)}</text>
              )}
            </g>
          )
        })}
        <line x1={P.l} x2={W - P.r} y1={y(perHour)} y2={y(perHour)}
              stroke="var(--text-2)" strokeWidth="1.5" strokeDasharray="5 4" />
        <text x={W - P.r} y={y(perHour) - 6} textAnchor="end" fontSize="10.5" fill="var(--text-2)">capacity</text>
      </svg>
      {tip && (
        <div className="chart-tip" style={{ left: tip.x, top: tip.y }}>
          <b>{label(tip.h.at)}</b> · {tip.h.n} alert{tip.h.n === 1 ? '' : 's'}
          {tip.over ? ' · over capacity' : ''}
        </div>
      )}
    </div>
  )
}
const AGE_ORDER = ['under 30m', '30m - 2h', '2h - 8h', '8h - 24h', 'over 24h']

export default function Operations() {
  const [data, setData] = useState(null)
  const [overview, setOverview] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const fetch = () =>
      api.operations()
        .then((d) => !cancelled && setData(d))
        .catch((e) => !cancelled && setError(e.message))
    const fetchOverview = () =>
      api.overview(24).then((d) => !cancelled && setOverview(d)).catch(() => {})
    fetch(); fetchOverview()
    const overviewTimer = setInterval(fetchOverview, 60000)
    const timer = setInterval(fetch, 20000)
    return () => { cancelled = true; clearInterval(timer); clearInterval(overviewTimer) }
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
      <div className="grid cols-4" style={{ marginBottom: 18 }}>
        <div className="deskstat" style={{ minWidth: 0 }}>
          <div className="badge">◎</div>
          <div className="k">Open cases</div>
          <div className="v">{totalCases}</div>
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>across all severities</div>
        </div>
        <div className="deskstat hero" style={{ minWidth: 0 }}>
          <div className="badge">₦</div>
          <div className="k">Money at risk</div>
          <div className="v">{nairaShort(totalExposure)}</div>
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>approved value, open cases</div>
        </div>
        <div className={`deskstat ${fpRate > 0.85 ? 'alarm' : ''}`} style={{ minWidth: 0 }}>
          <div className="badge">%</div>
          <div className="k">False positive rate</div>
          <div className="v">{Math.round(fpRate * 100)}%</div>
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>of {decided} decided cases</div>
        </div>
        <div className="deskstat" style={{ minWidth: 0 }}>
          <div className="badge">⧗</div>
          <div className="k">Time to resolve</div>
          <div className="v">
            {data.resolution?.median_minutes == null ? '—'
              : data.resolution.median_minutes >= 60
                ? `${Math.floor(data.resolution.median_minutes / 60)}h ${data.resolution.median_minutes % 60}m`
                : `${data.resolution.median_minutes}m`}
          </div>
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
            median, {data.resolution?.closed_7d ?? 0} cases closed in 7 days
          </div>
        </div>
        <div className="deskstat" style={{ minWidth: 0 }}>
          <div className="badge">◷</div>
          <div className="k">Oldest work</div>
          <div className="v" style={{ fontSize: 22 }}>
            {ageing.filter((a) => Number(a.cases) > 0).slice(-1)[0]?.bucket ?? '—'}
          </div>
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>bucket still holding cases</div>
        </div>
      </div>

      {overview && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="between wrap">
            <h2 style={{ margin: 0 }}>Alerts across the last day</h2>
            <span className={overview.alert_budget.utilisation > 1 ? 'risk risk-HIGH' : 'muted'}>
              {overview.alert_budget.last_24h} of {overview.alert_budget.per_day}
              {overview.alert_budget.utilisation > 1 ? ' · over capacity' : ' · within capacity'}
            </span>
          </div>
          <p className="dim" style={{ fontSize: 12, margin: '4px 0 12px' }}>
            Alerts raised each hour against the team's capacity of{' '}
            {Math.round(overview.alert_budget.per_day / 24)} an hour. Hover for the hour.
          </p>
          <AlertsVsCapacity overview={overview} />
          <div className="legend">
            <span><i style={{ background: 'var(--accent)' }} />Alerts raised</span>
            <span><i style={{ background: 'var(--high)' }} />Hour over capacity</span>
            <span><i style={{ borderTop: '2px dashed var(--text-2)', background: 'transparent',
                              height: 0, width: 14, borderRadius: 0 }} />Capacity</span>
          </div>
        </div>
      )}

      {/* D80: how the day's alerts were spent, tier by tier. */}
      {data.disposition_policy && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="between">
            <h2 style={{ margin: 0 }}>Who acted on today's alerts</h2>
            <span className="dim" style={{ fontSize: 12 }}>
              policy v{data.disposition_policy.version} · review capacity {data.disposition_policy.review_capacity_per_day}/day
            </span>
          </div>
          <div className="grid cols-3" style={{ marginTop: 10 }}>
            {[
              ['HUMAN_REVIEW', 'Analyst review', 'Investigated by a person'],
              ['MACHINE_ACTION', 'Machine action', `Hold or 24-hour flag, then a customer call · ${(data.disposition_policy.machine_action_signals || []).map((c) => c.replace(/_/g, ' ').toLowerCase()).join(', ')}`],
              ['AUTO_CLOSE', 'Auto-closed', data.disposition_policy.auto_close_enabled ? 'Recorded, no case' : 'Off: it would cost fraud value (D80)'],
            ].map(([key, label, note]) => (
              <div key={key}>
                <div className="stat-label">{label}</div>
                <div className="stat-value">{(data.tiers_24h || []).find((t) => t.disposition === key)?.n ?? 0}</div>
                <div className="stat-note">{note}</div>
              </div>
            ))}
          </div>
        </div>
      )}

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
            The alert budget assumes three analysts at twenty-five reviewable
            alerts each, leaving time for customer contact and regulatory clocks. If "open" climbs well past that, the thresholds need
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
