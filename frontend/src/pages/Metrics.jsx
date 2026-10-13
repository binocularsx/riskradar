import { useEffect, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

import GeoMap from '../components/GeoMap'
import { ago, api, when } from '../lib/api'
import { CHART, axis, grid, legend, tooltip } from '../lib/chart'
import { Banner, Empty, RiskBadge, Stat } from '../components/ui'
import { DECISION, OUTCOME, RULE, RULE_EFFECT, STAGE, say } from '../lib/words'

// The console's risk ramp (styles.css --low … --critical), as literals because
// Recharts writes them into SVG attributes.
const LEVEL_COLOUR = {
  LOW: '#059669', MEDIUM: '#d97706', HIGH: '#ea580c', CRITICAL: '#dc2626',
}
const LEVEL_NAME = { MEDIUM: 'Medium', HIGH: 'High', CRITICAL: 'Critical' }

const WINDOWS = [
  { hours: 6, label: 'Last 6 hours' },
  { hours: 24, label: 'Last 24 hours' },
  { hours: 168, label: 'Last 7 days' },
  { hours: 720, label: 'Last 30 days' },
]

// Buckets are hourly. Over more than a day the same "14:00" repeats on every
// date, so the label carries the day too.
function bucketLabel(value, hours) {
  const d = new Date(value)
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  return hours > 24 ? `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })} ${time}` : time
}

const DRIVER_NAME = { ...RULE, MODEL: 'The risk model on its own' }
const RESULT_NAME = OUTCOME
const STATE_NAME = STAGE
const EFFECT_NAME = RULE_EFFECT

/**
 * False alarms per rule and per model, from analyst outcomes (D69d, base PRD
 * FR-505). An aggregate rate hides the one rule that is drowning the desk.
 */
function FalseAlarmsByDriver({ detection }) {
  const rows = detection.by_driver
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>Alerts cleared as safe, by reason</h2>
      <p className="dim" style={{ fontSize: 12, marginTop: -6 }}>
        Completed cases from the last {detection.window_days} days, grouped by the reason the alert was raised.
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
              {TYPES.map((t) => <th key={`s${t}`} className="num">Known fraud · {TYPE_NAME[t]}</th>)}
              {TYPES.map((t) => <th key={`h${t}`} className="num">New fraud · {TYPE_NAME[t]}</th>)}
              <th className="num">New fraud, average</th>
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
                {TYPES.map((t) => {
                  const seen = o.seen_fraud?.[t]
                  return (
                    <td key={`s${t}`} className="num">
                      {seen ? `${seen.caught}/${seen.incidents}` : '—'}
                    </td>
                  )
                })}
                {/* Only the budget the desk actually runs was evaluated held-out;
                    the others carry held_out: null, and an unguarded read here
                    blanked the whole console. */}
                {TYPES.map((t) => {
                  const ho = o.held_out?.per_typology?.[t]
                  return (
                    <td key={`h${t}`} className="num"
                        title={ho?.ci95 ? `95% range ${ho.ci95.join('–')}` : 'not tested on fraud types it had not seen'}>
                      {ho?.recall != null ? ho.recall.toFixed(3) : '—'}
                    </td>
                  )
                })}
                <td className="num">
                  {o.held_out?.mean != null ? o.held_out.mean.toFixed(3) : '—'}
                </td>
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
        as false. Every row is a business choice; the desk runs at 75 a day.
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
export default function Metrics({ user }) {
  // The budget menu is where a budget is *chosen* from its threshold trade-offs
  // (D76), which is detection tuning and belongs to the administrator. A lead
  // needs to know the desk is over budget, not to re-pick the budget, so the
  // benchmark block is gated on the permission that actually tunes detection.
  const tunes = !!user?.permissions?.includes('admin:rules')
  const [hours, setHours] = useState(24)
  const [data, setData] = useState(null)
  const [detection, setDetection] = useState(null)
  const [menu, setMenu] = useState(null)
  const [error, setError] = useState(null)
  const [widened, setWidened] = useState(null)

  useEffect(() => {
    let cancelled = false
    api.overview(hours)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message))
    api.detection(30).then((d) => !cancelled && setDetection(d)).catch(() => {})
    if (tunes) api.budgetMenu().then((d) => !cancelled && setMenu(d)).catch(() => {})
    const timer = setInterval(() => {
      api.overview(hours).then((d) => !cancelled && setData(d)).catch(() => {})
    }, hours > 24 ? 60000 : 15000)  // a fixed refresh interval, not a socket (D14b); long windows are cached for a minute server-side
    return () => { cancelled = true; clearInterval(timer) }
  }, [hours, tunes])

  // Opening on an empty day shows nothing but "no data" boxes. Once, on first
  // load, widen to the last 30 days instead and say so; after that the window
  // is whatever the person picks.
  useEffect(() => {
    if (!data || widened !== null) return
    // Fewer than three hourly points is not a trend: a lone dot reads as broken.
    if (hours === 24 && data.transaction_volume.length < 3 && data.last_activity_at) {
      setWidened(true); setData(null); setHours(720)
    } else setWidened(false)
  }, [data, hours, widened])

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  const volume = data.transaction_volume.map((v) => ({
    bucket: bucketLabel(v.bucket, hours),
    transactions: Number(v.transactions),
    approved: Number(v.approved),
    declined: Number(v.declined),
  }))

  const alertsByHour = {}
  for (const row of data.alert_volume) {
    const key = bucketLabel(row.bucket, hours)
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
          <select id="window" style={{ width: 160 }} value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}>
            {WINDOWS.map((w) => <option key={w.hours} value={w.hours}>{w.label}</option>)}
          </select>
        </div>
        <span className="dim" style={{ fontSize: 12 }}>refreshes every {hours > 24 ? 'minute' : '15 seconds'}</span>
      </div>

      {widened && hours === 720 && volume.length > 0 && (
        <Banner kind="info">
          Showing the last 30 days, because almost nothing arrived in the last 24 hours. The most recent
          payment was {ago(data.last_activity_at)} ({when(data.last_activity_at)}). Pick another window above to change it.
        </Banner>
      )}
      {!volume.length && (
        <Banner kind="warn">
          No payments arrived in the {WINDOWS.find((w) => w.hours === hours)?.label.toLowerCase()}, so the charts
          below are empty.{' '}
          {data.last_activity_at
            ? <>The most recent payment the system received was {ago(data.last_activity_at)} ({when(data.last_activity_at)}).{' '}</>
            : <>The system has not received a payment yet.{' '}</>}
          {hours < 720 && data.last_activity_at && (
            <button type="button" className="linkish" onClick={() => setHours(720)}>Show the last 30 days</button>
          )}
        </Banner>
      )}

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat
          label="Daily review limit"
          value={`${budget.last_24h} / ${budget.per_day}`}
          note={`${Math.round(utilisation * 100)}% of a ${budget.per_day}/day desk`}
        />
        <Stat
          label="Decision time"
          value={data.latency.p95_ms != null ? `${data.latency.p95_ms} ms` : '—'}
          note={`95% of checks · target under 2 seconds · ${scored.toLocaleString()} checked`}
        />
        <Stat
          label="Items waiting"
          value={data.queue.depth}
          note={`oldest ${Math.round(Number(data.queue.oldest_seconds))}s`}
        />
        <Stat
          label="Detection system"
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
          <h2 style={{ margin: 0 }}>Daily review limit</h2>
          <span className={utilisation > 1 ? 'risk risk-HIGH' : 'muted'}>
            {utilisation > 1 ? 'more than the team can review' : 'within the team limit'}
          </span>
        </div>
        <div className="bar-track">
          <div className="bar-fill" style={{
            width: `${Math.min(100, utilisation * 100)}%`,
            background: utilisation > 1 ? 'var(--high)' : 'var(--accent)',
          }} />
        </div>
        <p className="dim" style={{ fontSize: 12, marginBottom: 0, marginTop: 8 }}>
          This limit is based on three team members reviewing twenty-five alerts each day.
          Going over it means new work is arriving faster than the team can review it.
        </p>
      </div>

      {tunes && menu?.available && <Benchmarks menu={menu} live={detection?.ratio} />}

      <div className="grid cols-2">
        <div className="card">
          <h2>Transaction volume</h2>
          {volume.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={volume} margin={{ top: 6, right: 8, left: -6, bottom: 0 }}>
                <CartesianGrid {...grid} />
                <XAxis dataKey="bucket" {...axis} minTickGap={28} />
                <YAxis {...axis} width={52} />
                <Tooltip {...tooltip} />
                <Legend {...legend} />
                <Area type="monotone" dataKey="approved" stackId="1"
                      stroke={CHART.blue} fill={CHART.blueFill} fillOpacity={0.9} name="Approved" />
                <Area type="monotone" dataKey="declined" stackId="1"
                      stroke={CHART.orange} fill={CHART.orangeFill} fillOpacity={0.95} name="Declined or failed" />
              </AreaChart>
            </ResponsiveContainer>
          ) : <Empty>No transactions in this window.</Empty>}
        </div>

        <div className="card">
          <h2>Alerts by risk level</h2>
          {alerts.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={alerts} margin={{ top: 6, right: 8, left: -6, bottom: 0 }}>
                <CartesianGrid {...grid} />
                <XAxis dataKey="bucket" {...axis} minTickGap={28} />
                <YAxis {...axis} allowDecimals={false} width={40} />
                <Tooltip {...tooltip} />
                <Legend {...legend} />
                {['MEDIUM', 'HIGH', 'CRITICAL'].map((level) => (
                  <Bar key={level} dataKey={level} name={LEVEL_NAME[level]} stackId="a" fill={LEVEL_COLOUR[level]} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty>No alerts in this window.</Empty>}
        </div>

        <div className="card">
          <h2>Reasons alerts were raised</h2>
          {data.signal_frequency.length ? (
            <table>
              <thead><tr><th>Reason</th><th>Effect</th><th className="num">Count</th></tr></thead>
              <tbody>
                {data.signal_frequency.map((s) => (
                  <tr key={s.code}>
                    <td>{say(DRIVER_NAME, s.code)}</td>
                    <td><span className={`pill ${String(s.power).toLowerCase()}`}>{EFFECT_NAME[s.power] || s.power}</span></td>
                    <td className="num">{s.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No alert reasons recorded in this period.</Empty>}
        </div>

        <div className="card">
          <h2>Investigation results</h2>
          {data.case_outcomes.length ? (
            <table>
              <thead><tr><th>Outcome</th><th>State</th><th className="num">Cases</th></tr></thead>
              <tbody>
                {data.case_outcomes.map((o, i) => (
                  <tr key={i}>
                    <td>{o.outcome === 'PENDING' ? 'Not decided yet' : say(RESULT_NAME, o.outcome)}</td>
                    <td className="muted">{say(STATE_NAME, o.state)}</td>
                    <td className="num">{o.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No cases yet.</Empty>}
          <p className="dim" style={{ fontSize: 11, marginBottom: 0 }}>
            These results come from completed reviews by the investigation team.
          </p>
        </div>
      </div>

      <div style={{ marginTop: 16 }}><GeoMap showLink={!!user?.permissions?.includes('analysis:geography')} /></div>

      {detection && <FalseAlarmsByDriver detection={detection} />}

      <div className="card" style={{ marginTop: 16 }}>
        <h2>System decisions</h2>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Risk level</th><th>Decision</th><th className="num">Decisions</th></tr></thead>
            <tbody>
              {data.risk_mix.map((r, i) => (
                <tr key={i}>
                  <td><RiskBadge level={r.risk_level} /></td>
                  <td>{say(DECISION, r.decision)}</td>
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
