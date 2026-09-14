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

const TYPE_NAME = {
  ACCOUNT_TAKEOVER: 'Takeover', MULE_FANOUT: 'Mule ring', CARD_TESTING: 'Card testing',
}
const TYPES = Object.keys(TYPE_NAME)
const SCALE_MAX = 36

const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`)
const ratio = (v) => (v == null ? '—' : `${v}:1`)

/**
 * Where each budget sits against published bank figures: false alerts per
 * confirmed fraud incident on a 0–36 scale, best-in-class band shaded.
 */
function RatioScale({ options, band, strong }) {
  const at = (v) => `${(Math.min(v, SCALE_MAX) / SCALE_MAX) * 100}%`
  return (
    <div style={{ position: 'relative', height: 64, margin: '8px 0 4px' }}>
      <div style={{ position: 'absolute', top: 30, left: 0, right: 0, height: 6,
                    borderRadius: 3, background: 'var(--surface-3)' }} />
      <div title="Strong banks" style={{ position: 'absolute', top: 30, height: 6, left: at(band[1]),
                    width: `calc(${at(strong)} - ${at(band[1])})`,
                    background: 'color-mix(in srgb, var(--medium) 35%, transparent)' }} />
      <div title="Best-in-class" style={{ position: 'absolute', top: 24, height: 18, left: at(band[0]),
                    width: `calc(${at(band[1])} - ${at(band[0])})`, borderRadius: 3,
                    background: 'color-mix(in srgb, var(--low) 45%, transparent)' }} />
      {options.map((o) => (
        <div key={o.budget_per_day} style={{ position: 'absolute', left: at(o.false_alerts_per_incident),
                                             top: o.current ? 0 : 44, transform: 'translateX(-50%)',
                                             textAlign: 'center', whiteSpace: 'nowrap', fontSize: 11 }}>
          <span className={o.current ? '' : 'dim'} style={{ fontWeight: o.current ? 650 : 400 }}>
            {o.budget_per_day}/day · {ratio(o.false_alerts_per_incident)}
          </span>
        </div>
      ))}
      {options.map((o) => (
        <div key={`m${o.budget_per_day}`} style={{ position: 'absolute', left: at(o.false_alerts_per_incident),
                                                  top: 26, width: 2, height: 14, transform: 'translateX(-50%)',
                                                  background: o.current ? 'var(--accent)' : 'var(--text-3)' }} />
      ))}
    </div>
  )
}

/**
 * WP-09: the two numbers banks benchmark on, and the budget menu (D67c).
 *
 * Counts of false alarms read as failure; the same system, reported as false
 * alerts per confirmed incident and share of fraud value detected, sits in the
 * band banks advertise. The menu is display only: the budget was chosen from it
 * in D76 and lives in app_config.
 */
function Benchmarks({ menu, live }) {
  const current = menu.options.find((o) => o.current)
  const bench = menu.benchmarks || {}
  const band = bench.false_alerts_per_incident?.best_in_class || [12, 14]
  const strong = bench.false_alerts_per_incident?.strong_banks_up_to || 30
  const vdrBand = bench.value_detection_rate?.market_leading || [0.6, 0.7]

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2>What banks benchmark</h2>
      <p className="dim" style={{ fontSize: 12, marginTop: -6 }}>
        Banks compare fraud systems on false alerts per confirmed fraud incident and on the share
        of fraud value detected, not on counts of false alarms.
      </p>

      <div className="grid cols-3" style={{ marginBottom: 12 }}>
        <Stat
          label={`False alerts per incident · ${menu.current_budget_per_day}/day`}
          value={current ? ratio(current.false_alerts_per_incident) : '—'}
          note={current
            ? `Evaluation, ${menu.test_days} days · best-in-class ${band[0]}–${band[1]}:1`
            : `${menu.current_budget_per_day}/day has not been measured`}
        />
        <Stat
          label={`Fraud value detected · ${menu.current_budget_per_day}/day`}
          value={current ? pct(current.value_detection_rate) : '—'}
          note={current
            ? `${pct(current.value_in_caught_incidents)} in incidents caught · market-leading ${pct(vdrBand[0])}–${pct(vdrBand[1])}`
            : 'not measured'}
        />
        <Stat
          label="False alerts per incident · live, 30 days"
          value={live ? ratio(live.false_alerts_per_incident) : '—'}
          note={live
            ? (live.enough_evidence
              ? `${live.false_alerts} false alerts, ${live.confirmed_incidents} confirmed incidents`
              : `too few to rate — ${live.confirmed_incidents} confirmed incident${live.confirmed_incidents === 1 ? '' : 's'}, under 30`)
            : 'no decided cases'}
        />
      </div>

      {current && <RatioScale options={menu.options} band={band} strong={strong} />}
      <div className="legend" style={{ marginBottom: 14 }}>
        <span><i style={{ background: 'color-mix(in srgb, var(--low) 45%, transparent)' }} />Best-in-class {band[0]}–{band[1]}:1</span>
        <span><i style={{ background: 'color-mix(in srgb, var(--medium) 35%, transparent)' }} />Strong banks, up to {strong}:1</span>
      </div>

      <h2 style={{ fontSize: 14 }}>Budget options</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Budget</th>
              <th className="num">False alerts / day</th>
              <th className="num">Per incident</th>
              <th className="num">Value detected</th>
              {TYPES.map((t) => <th key={`s${t}`} className="num">Seen · {TYPE_NAME[t]}</th>)}
              {TYPES.map((t) => <th key={`h${t}`} className="num">Unseen · {TYPE_NAME[t]}</th>)}
              <th className="num">Unseen mean</th>
            </tr>
          </thead>
          <tbody>
            {menu.options.map((o) => (
              <tr key={o.budget_per_day}>
                <td>
                  {o.budget_per_day} / day{' '}
                  {o.current && <span className="pill" style={{ color: 'var(--accent)' }}>today</span>}
                </td>
                <td className="num">{o.false_alerts_per_day}</td>
                <td className="num">{ratio(o.false_alerts_per_incident)}</td>
                <td className="num">{pct(o.value_detection_rate)}</td>
                {TYPES.map((t) => (
                  <td key={`s${t}`} className="num">{o.seen_fraud[t].caught}/{o.seen_fraud[t].incidents}</td>
                ))}
                {TYPES.map((t) => (
                  <td key={`h${t}`} className="num"
                      title={`95% range ${o.held_out.per_typology[t].ci95.join('–')}`}>
                    {o.held_out.per_typology[t].recall.toFixed(3)}
                  </td>
                ))}
                <td className="num">{o.held_out.mean.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="dim" style={{ fontSize: 11.5, marginBottom: 0 }}>
        Seen: fraud types the model trained on, newest {menu.test_days} days of the corpus. Unseen:
        incident recall with that fraud type hidden from training. Value detected counts money on
        alerted fraud payments only; the live desk cannot measure it, because the fraud it misses
        carries no label. The live ratio is a floor: alerts inside a confirmed case are never counted
        as false. Every row is a business choice; the desk runs at 75 a day (D76).
      </p>
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
  const [menu, setMenu] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api.overview(hours)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message))
    api.detection(30).then((d) => !cancelled && setDetection(d)).catch(() => {})
    api.budgetMenu().then((d) => !cancelled && setMenu(d)).catch(() => {})
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
          three analysts at twenty-five reviewable alerts each. Going over it does not
          mean more fraud; it means the desk cannot keep up.
        </p>
      </div>

      {menu?.available && <Benchmarks menu={menu} live={detection?.ratio} />}

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
