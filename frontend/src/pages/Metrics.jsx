import { useEffect, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

import { api, naira } from '../lib/api'
import { Banner, Empty, RiskBadge, Stat } from '../components/ui'

const LEVEL_COLOUR = {
  LOW: '#3f9f6a', MEDIUM: '#ddc24e', HIGH: '#e2692c', CRITICAL: '#b93b6a',
}

const axis = { stroke: '#5f6b7a', fontSize: 11 }
const tooltip = {
  contentStyle: {
    background: '#151b23', border: '1px solid #232d3a', borderRadius: 8, fontSize: 12,
  },
}

function hour(value) {
  return new Date(value).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

const DRIVER_NAME = {
  MODEL: 'The model alone',
  VELOCITY_BURST_1H: 'Burst of payments to a new destination',
  CARD_TESTING_PROBES: 'Card-testing probes',
  SANCTIONED_BENEFICIARY: 'Sanctioned destination',
  KNOWN_MULE_BENEFICIARY: 'Known mule destination',
}

/**
 * False alarms per rule and per model, from analyst outcomes (D69d, base PRD
 * FR-505). An aggregate rate hides the one rule that is drowning the desk.
 */
function FalseAlarmsByDriver({ detection }) {
  const rows = detection.by_driver
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>False alarms by rule and by model</h2>
      <p className="dim" style={{ fontSize: 12, marginTop: -6 }}>
        Decided cases in the last {detection.window_days} days, by what put them in front of an
        analyst. A case where two rules fired counts for both; "can't tell" is left out of the rate.
      </p>
      {rows.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Raised by</th><th className="num">Cases</th><th className="num">Fraud</th>
                  <th className="num">False alarm</th><th className="num">Can't tell</th>
                  <th className="num">Right</th><th>95% range</th><th>Evidence</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.driver}>
                  <td>{DRIVER_NAME[r.driver] || r.driver}</td>
                  <td className="num">{r.cases}</td>
                  <td className="num">{r.confirmed_fraud}</td>
                  <td className="num">{r.false_positive}</td>
                  <td className="num">{r.inconclusive}</td>
                  <td className="num">{r.precision == null ? '—' : `${Math.round(r.precision * 100)}%`}</td>
                  <td className="mono dim">{r.precision_ci95 ? `${Math.round(r.precision_ci95[0] * 100)}–${Math.round(r.precision_ci95[1] * 100)}%` : '—'}</td>
                  <td>{r.enough_evidence
                    ? <span className="muted">enough</span>
                    : <span className="risk risk-MEDIUM">thin — under {detection.min_decided_for_evidence} cases</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <Empty>No decided cases in this window.</Empty>}
    </div>
  )
}

/**
 * Aggregates (FR-032, FR-034).
 *
 * D14b: transactions are charts and counters, never a live feed. Alert volume is
 * low by construction, so alerts stream; transaction volume is three orders of
 * magnitude larger and belongs here.
 */
export default function Metrics() {
  const [hours, setHours] = useState(24)
  const [data, setData] = useState(null)
  const [detection, setDetection] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api.overview(hours)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message))
    api.detection(30).then((d) => !cancelled && setDetection(d)).catch(() => {})
    const timer = setInterval(() => {
      api.overview(hours).then((d) => !cancelled && setData(d)).catch(() => {})
    }, 15000)  // a fixed refresh interval, not a socket (D14b)
    return () => { cancelled = true; clearInterval(timer) }
  }, [hours])

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  const volume = data.transaction_volume.map((v) => ({
    bucket: hour(v.bucket),
    transactions: Number(v.transactions),
    approved: Number(v.approved),
    declined: Number(v.declined),
  }))

  const alertsByHour = {}
  for (const row of data.alert_volume) {
    const key = hour(row.bucket)
    alertsByHour[key] = alertsByHour[key] || { bucket: key, LOW: 0, MEDIUM: 0, HIGH: 0, CRITICAL: 0 }
    alertsByHour[key][row.risk_level] = Number(row.alerts)
  }
  const alerts = Object.values(alertsByHour)

  const budget = data.alert_budget
  const utilisation = budget.utilisation ?? 0
  const scored = Number(data.latency.scored || 0)

  return (
    <>
      <div className="between" style={{ marginBottom: 14 }}>
        <div className="row">
          <label style={{ margin: 0 }} htmlFor="window">Window</label>
          <select id="window" style={{ width: 140 }} value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}>
            <option value={6}>Last 6 hours</option>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
          </select>
        </div>
        <span className="dim" style={{ fontSize: 12 }}>refreshes every 15s</span>
      </div>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat
          label="Alert budget"
          value={`${budget.last_24h} / ${budget.per_day}`}
          note={`${Math.round(utilisation * 100)}% of a ${budget.per_day}/day desk`}
        />
        <Stat
          label="p95 decision latency"
          value={data.latency.p95_ms != null ? `${data.latency.p95_ms} ms` : '—'}
          note={`NFR-001 target < 2000 ms · ${scored} scored`}
        />
        <Stat
          label="Queue depth"
          value={data.queue.depth}
          note={`oldest ${Math.round(Number(data.queue.oldest_seconds))}s`}
        />
        <Stat
          label="Active model"
          value={data.active_model ? data.active_model.name : 'none'}
          note={data.active_model
            ? `${data.active_model.version} · ${data.active_model.calibration}`
            : 'rule-only mode'}
        />
      </div>

      {/* The budget is the number every threshold was solved backwards from
          (D11d), so it gets a bar rather than a buried figure. */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="between" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Alert budget utilisation</h2>
          <span className={utilisation > 1 ? 'risk risk-HIGH' : 'muted'}>
            {utilisation > 1 ? 'over budget' : 'within budget'}
          </span>
        </div>
        <div className="bar-track">
          <div className="bar-fill" style={{
            width: `${Math.min(100, utilisation * 100)}%`,
            background: utilisation > 1 ? 'var(--high)' : 'var(--accent)',
          }} />
        </div>
        <p className="dim" style={{ fontSize: 12, marginBottom: 0, marginTop: 8 }}>
          Every threshold in the system was derived backwards from this number —
          three analysts at forty reviewable alerts each. Going over it does not
          mean more fraud; it means the desk cannot keep up.
        </p>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>Transaction volume</h2>
          {volume.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={volume}>
                <CartesianGrid stroke="#232d3a" vertical={false} />
                <XAxis dataKey="bucket" {...axis} />
                <YAxis {...axis} />
                <Tooltip {...tooltip} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Area type="monotone" dataKey="approved" stackId="1"
                      stroke="#3b82f6" fill="#1e3a5f" name="Approved" />
                <Area type="monotone" dataKey="declined" stackId="1"
                      stroke="#d97036" fill="#3a2318" name="Declined / failed" />
              </AreaChart>
            </ResponsiveContainer>
          ) : <Empty>No transactions in this window.</Empty>}
        </div>

        <div className="card">
          <h2>Alerts by risk level</h2>
          {alerts.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={alerts}>
                <CartesianGrid stroke="#232d3a" vertical={false} />
                <XAxis dataKey="bucket" {...axis} />
                <YAxis {...axis} allowDecimals={false} />
                <Tooltip {...tooltip} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {['MEDIUM', 'HIGH', 'CRITICAL'].map((level) => (
                  <Bar key={level} dataKey={level} stackId="a" fill={LEVEL_COLOUR[level]} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty>No alerts in this window.</Empty>}
        </div>

        <div className="card">
          <h2>Rules fired</h2>
          {data.signal_frequency.length ? (
            <table>
              <thead><tr><th>Signal</th><th>Power</th><th className="num">Count</th></tr></thead>
              <tbody>
                {data.signal_frequency.map((s) => (
                  <tr key={s.code}>
                    <td className="mono">{s.code}</td>
                    <td><span className={`pill ${String(s.power).toLowerCase()}`}>{s.power}</span></td>
                    <td className="num">{s.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No rules fired in this window.</Empty>}
        </div>

        <div className="card">
          <h2>Case outcomes</h2>
          {data.case_outcomes.length ? (
            <table>
              <thead><tr><th>Outcome</th><th>State</th><th className="num">Cases</th></tr></thead>
              <tbody>
                {data.case_outcomes.map((o, i) => (
                  <tr key={i}>
                    <td>{o.outcome.replace(/_/g, ' ')}</td>
                    <td className="muted">{o.state.replace(/_/g, ' ')}</td>
                    <td className="num">{o.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No cases yet.</Empty>}
          <p className="dim" style={{ fontSize: 11, marginBottom: 0 }}>
            Confirmed fraud and false positive are analyst-labelled ground truth —
            the only non-circular labels this system produces, and the eventual
            exit from simulator-only training.
          </p>
        </div>
      </div>

      {detection && <FalseAlarmsByDriver detection={detection} />}

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Decision mix</h2>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Risk level</th><th>Decision</th><th className="num">Decisions</th></tr></thead>
            <tbody>
              {data.risk_mix.map((r, i) => (
                <tr key={i}>
                  <td><RiskBadge level={r.risk_level} /></td>
                  <td className="mono">{r.decision}</td>
                  <td className="num">{r.n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.latency.rule_only > 0 && (
          <Banner kind="warn">
            {data.latency.rule_only} decision(s) in this window were scored in
            rule-only mode — the model was unavailable.
          </Banner>
        )}
      </div>
    </>
  )
}
