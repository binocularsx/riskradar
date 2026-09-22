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
  const critical = Number(backlog.find((b) => b.risk_level === 'CRITICAL')?.cases || 0)

  const ageing = AGE_ORDER.map(
    (b) => data.ageing.find((a) => a.bucket === b) || { bucket: b, cases: 0 }
  )
  const maxAge = Math.max(...ageing.map((a) => Number(a.cases)), 1)

  const outcomes = data.outcomes || []
  const decided = data.decided || 1
  const fpRate = data.false_positive_rate
  const budgetUsed = overview ? Math.round(overview.alert_budget.utilisation * 100) : null
  const avgWait = totalCases ? Math.round(backlog.reduce((a, b) => a + Number(b.avg_age_minutes) * Number(b.cases), 0) / totalCases) : 0
  const outColour = (o) => o === 'CONFIRMED_FRAUD' ? 'var(--critical)' : o === 'FALSE_POSITIVE' ? 'var(--low)' : 'var(--medium)'

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="page-sub">Monitor fraud activity, workload and investigation performance.</p>
        </div>
        <span className="live"><span className="live-dot on" /> Today (live)</span>
      </div>

      <div className="statrow">
        <div className="statcard">
          <div className="statcard-k">Open cases</div>
          <div className="statcard-v">{totalCases}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>{critical} require urgent triage</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">Alert budget used</div>
          <div className="statcard-v accent">{budgetUsed == null ? '—' : `${budgetUsed}%`}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
            {overview ? `${overview.alert_budget.last_24h} of ${overview.alert_budget.per_day} daily` : ''}</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">False positive rate</div>
          <div className={`statcard-v ${fpRate > 0.85 ? 'danger' : ''}`}>{Math.round(fpRate * 100)}%</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>of {decided} decided cases</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">Queue depth</div>
          <div className="statcard-v">{totalCases}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>avg wait {avgWait >= 60 ? `${Math.round(avgWait / 60)}h` : `${avgWait} min`}</div>
        </div>
      </div>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <div className="card">
          <h2>Alert volume vs capacity</h2>
          <p className="dim" style={{ fontSize: 12, margin: '2px 0 12px' }}>
            Hourly ingested alerts vs analyst team review throughput
            {overview ? ` (${Math.round(overview.alert_budget.per_day / 24)}/h)` : ''}.</p>
          {overview ? <AlertsVsCapacity overview={overview} /> : <p className="dim">Loading…</p>}
        </div>

        <div className="card">
          <div className="between"><h2 style={{ margin: 0 }}>Backlog by risk</h2>
            <span className="dim" style={{ fontSize: 12 }}>Total {totalCases} cases</span></div>
          <div style={{ marginTop: 12 }}>
            {backlog.map((b) => {
              const target = data.sla_minutes[b.risk_level]
              const over = Number(b.avg_age_minutes) > target
              const pct = totalCases ? Math.round((Number(b.cases) / totalCases) * 100) : 0
              return (
                <div key={b.risk_level} style={{ marginBottom: 14 }}>
                  <div className="between" style={{ fontSize: 12.5, marginBottom: 5 }}>
                    <span><RiskBadge level={b.risk_level} /> <strong style={{ marginLeft: 6 }}>{b.cases} cases</strong> <span className="dim">({pct}%)</span></span>
                    <span className={over ? 'risk risk-HIGH' : 'dim'} style={{ fontSize: 11.5 }}>
                      {over ? 'over SLA' : 'compliant'}</span>
                  </div>
                  <div className="bar-track">
                    <div className="bar-fill" style={{ width: `${pct}%`,
                      background: b.risk_level === 'CRITICAL' ? 'var(--critical)' : b.risk_level === 'HIGH' ? 'var(--high)' : b.risk_level === 'MEDIUM' ? 'var(--medium)' : 'var(--low)' }} />
                  </div>
                  <div className="dim" style={{ fontSize: 10.5, marginTop: 3 }}>
                    Target SLA: {target >= 60 ? `${target / 60}h` : `${target} min`}</div>
                </div>
              )
            })}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: 0, marginBottom: 16 }}>
        <div className="toolbar">
          <div><strong style={{ fontSize: 14 }}>Analyst workload</strong>
            <div className="dim" style={{ fontSize: 12 }}>Real-time queue load and 24-hour closure velocity</div></div>
          <span className="live"><span className="live-dot on" /> {data.analysts.length} active</span>
        </div>
        <div className="table-scroll">
          <table className="rowtable">
            <thead><tr><th>Analyst</th><th>Role</th><th className="num">Open cases</th><th className="num">Closed 24h</th><th>SLA / status</th></tr></thead>
            <tbody>
              {data.analysts.map((a) => {
                const strained = Number(a.open_cases) > 10
                return (
                  <tr key={a.display_name}>
                    <td><div className="userpair"><span className="avatar sm">{(a.display_name || '?').split(/\s+/).slice(0, 2).map((s) => s[0]).join('').toUpperCase()}</span>
                      <span style={{ fontWeight: 560 }}>{a.display_name}</span></div></td>
                    <td className="muted" style={{ textTransform: 'capitalize' }}>{a.role.replace(/_/g, ' ').toLowerCase()}</td>
                    <td className="num">{a.open_cases}</td>
                    <td className="num">{a.closed_24h}</td>
                    <td><span className={`authdot`}><span className={`live-dot ${strained ? 'warn' : 'ok'}`} />{strained ? 'At capacity' : 'On track'}</span></td>
                  </tr>
                )
              })}
              {!data.analysts.length && <tr><td colSpan={5} className="dim" style={{ textAlign: 'center', padding: 24 }}>No active analysts.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="between"><h2 style={{ margin: 0 }}>Case outcomes (past period)</h2>
          <span className="dim" style={{ fontSize: 12 }}>{decided} completed investigations</span></div>
        <div className="outcome-bar" style={{ marginTop: 12 }}>
          {outcomes.map((o) => (
            <span key={o.outcome} className="outcome-seg"
                  style={{ flex: Number(o.n) || 0.001, background: outColour(o.outcome) }}
                  title={`${o.outcome.replace(/_/g, ' ').toLowerCase()}: ${o.n}`} />
          ))}
        </div>
        <div className="row wrap" style={{ gap: 18, marginTop: 12 }}>
          {outcomes.map((o) => (
            <span key={o.outcome} style={{ fontSize: 12.5 }}>
              <i style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 3, background: outColour(o.outcome), marginRight: 6, verticalAlign: -1 }} />
              {o.outcome.replace(/_/g, ' ').toLowerCase()}: <strong>{o.n}</strong>{' '}
              <span className="dim">({Math.round((Number(o.n) / decided) * 100)}%)</span>
            </span>
          ))}
          {!outcomes.length && <span className="dim">No cases decided yet.</span>}
        </div>
      </div>
    </div>
  )
}

