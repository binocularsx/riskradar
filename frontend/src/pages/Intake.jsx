import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Area, Bar, CartesianGrid, ComposedChart, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'
import { CHANNEL, RULE, say } from '../lib/words'

/**
 * The live desk (D83): what is arriving, what is waiting, what was flagged, and
 * what to expect — before anyone opens a case.
 *
 * Refreshed every five seconds. Every number says what window it covers,
 * because "12 alerts" means nothing until you know whether that was a minute
 * or a day.
 */

const WINDOWS = [30, 60, 180]
const axis = { stroke: '#61748a', fontSize: 11 }
const tip = { background: '#141c28', border: '1px solid #2c3d50', borderRadius: 8, fontSize: 12 }
const LEVEL_COLOR = { LOW: 'var(--low)', MEDIUM: 'var(--medium)', HIGH: 'var(--high)', CRITICAL: 'var(--critical)' }
const TIER = { HUMAN_REVIEW: 'Sent to an analyst', MACHINE_ACTION: 'Machine action', AUTO_CLOSE: 'Auto-closed', NONE: 'No action needed' }

function Kpi({ label, value, note, tone }) {
  return (
    <div className={`kpi ${tone || ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  )
}

function hhmm(value) {
  return new Date(value).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

function Bars({ rows, colour, total }) {
  const max = Math.max(1, ...rows.map((r) => r.n))
  return (
    <div className="hbars">
      {rows.map((r) => (
        <div className="hbar" key={r.key}>
          <span className="hbar-label">{r.label}</span>
          <span className="hbar-track">
            <span className="hbar-fill" style={{ width: `${(r.n / max) * 100}%`, background: colour?.(r) || 'var(--accent)' }} />
          </span>
          <span className="num hbar-n">{r.n.toLocaleString()}{total ? <span className="dim"> · {Math.round((100 * r.n) / Math.max(total, 1))}%</span> : null}</span>
        </div>
      ))}
      {!rows.length && <p className="dim" style={{ margin: 0 }}>Nothing in this window yet.</p>}
    </div>
  )
}

export default function Intake() {
  const [minutes, setMinutes] = useState(60)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () => api.intake(minutes)
      .then((d) => { if (alive) { setData(d); setError(null) } })
      .catch((e) => alive && setError(e.message))
    load()
    const timer = setInterval(load, 5000)
    return () => { alive = false; clearInterval(timer) }
  }, [minutes])

  if (!data) return error ? <Banner kind="error">{error}</Banner> : <p className="dim">Loading the live desk…</p>

  const w = data.window
  const last = data.series.slice(-5)
  const perMinute = Math.round(last.reduce((s, r) => s + r.received, 0) / Math.max(last.length, 1))
  const levels = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((k) => ({ key: k, label: k.toLowerCase(), n: data.by_risk_level[k] || 0 }))
  const scoredTotal = levels.reduce((s, r) => s + r.n, 0)
  const q = data.queue
  const b = data.budget
  const overBudget = b.alerts_per_day && b.projected_today > b.alerts_per_day

  return (
    <div className="intake">
      {error && <Banner kind="error">{error}</Banner>}
      <div className="between wrap" style={{ marginBottom: 14 }}>
        <p className="muted" style={{ margin: 0, maxWidth: 720 }}>
          Transactions arriving right now, before anyone opens a case: how many, how quickly they are
          scored, what was flagged and why, and what the next hour should bring. Refreshes every five seconds.
        </p>
        <div className="segmented">
          {WINDOWS.map((m) => (
            <button key={m} className={m === minutes ? 'on' : ''} onClick={() => setMinutes(m)}>
              last {m >= 60 ? `${m / 60}h` : `${m}m`}
            </button>
          ))}
        </div>
      </div>

      <div className="kpis">
        <Kpi label={`Received · last ${minutes}m`} value={Number(w.received).toLocaleString()}
             note={`${perMinute}/min now · ${nairaShort(w.received_value_minor)} · ${w.credits} credits`} />
        <Kpi label="Waiting for a risk check" value={q.waiting.toLocaleString()} tone={q.waiting > 200 ? 'warn' : ''}
             note={q.waiting ? `oldest ${Math.round(q.oldest_seconds)}s` : 'nothing waiting'} />
        <Kpi label="Risk-check time" value={`${q.scoring_lag_seconds_p50}s`}
             note={`95% within ${q.scoring_lag_seconds_p95}s · target 2s`} tone={q.scoring_lag_seconds_p95 > 2 ? 'warn' : ''} />
        <Kpi label={`Flagged · last ${minutes}m`} value={Number(w.alerts).toLocaleString()}
             note={`${w.scored ? ((100 * w.alerts) / w.scored).toFixed(2) : '0'}% of scored · ${nairaShort(w.flagged_value_minor)} at risk`} />
        <Kpi label={`Cases · last ${minutes}m`} value={`${w.cases_opened} opened`} note={`${w.cases_closed} closed`} />
        <Kpi label="Expected in the next hour" value={`~${data.expected_next_hour.alerts} alerts`}
             note={`~${data.expected_next_hour.received.toLocaleString()} transactions, at ${data.expected_next_hour.basis}`} />
        <Kpi label="Daily review limit" value={b.alerts_per_day ? `${b.alerts_today} / ${b.alerts_per_day}` : b.alerts_today}
             tone={overBudget ? 'warn' : ''}
             note={b.alerts_per_day ? `on pace for ${b.projected_today} by midnight` : 'no daily limit set'} />
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="between"><h2 style={{ margin: 0 }}>Transactions received, checked and flagged</h2>
          <span className="dim" style={{ fontSize: 12 }}>orange bars show alerts</span></div>
        <div style={{ height: 260, marginTop: 10 }}>
          <ResponsiveContainer>
            <ComposedChart data={data.series} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="#1f2a38" vertical={false} />
              <XAxis dataKey="minute" tickFormatter={hhmm} {...axis} minTickGap={40} />
              <YAxis yAxisId="left" {...axis} allowDecimals={false} />
              <YAxis yAxisId="right" orientation="right" {...axis} allowDecimals={false} />
              <Tooltip contentStyle={tip} labelFormatter={hhmm} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Area yAxisId="left" type="monotone" dataKey="received" name="received" stroke="#4da3ff" fill="#4da3ff" fillOpacity={0.18} />
              <Area yAxisId="left" type="monotone" dataKey="scored" name="scored" stroke="#7cc0ff" fill="transparent" strokeDasharray="4 3" />
              <Bar yAxisId="right" dataKey="alerts" name="alerts" fill="#e2692c" barSize={6} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="grid cols-3" style={{ marginTop: 14 }}>
        <div className="card">
          <h3>Risk level of checked transactions</h3>
          <Bars rows={levels} total={scoredTotal} colour={(r) => LEVEL_COLOR[r.key.toUpperCase()]} />
          <p className="dim" style={{ fontSize: 11.5, margin: '10px 0 0' }}>
            Medium and above can raise an alert; the daily review limit decides how many reach the team.
          </p>
        </div>
        <div className="card">
          <h3>Why alerts were raised</h3>
          <Bars rows={data.signals.map((s) => ({ key: s.code, label: say(RULE, s.code), n: s.n }))}
                colour={(r) => (data.signals.find((s) => s.code === r.key)?.power === 'SUPPRESS' ? 'var(--low)' : 'var(--high)')} />
          <p className="dim" style={{ fontSize: 11.5, margin: '10px 0 0' }}>
            Orange reasons increase concern; green reasons reduce it. Some alerts come from the detection system itself.
          </p>
        </div>
        <div className="card">
          <h3>Who handles it</h3>
          <Bars rows={Object.entries(data.by_disposition).map(([k, n]) => ({ key: k, label: TIER[k] || k, n }))} />
          <h3 style={{ marginTop: 16 }}>By channel</h3>
          <Bars rows={data.by_channel.map((c) => ({ key: c.channel, label: `${say(CHANNEL, c.channel)}${c.alerted ? ` · ${c.alerted} flagged` : ''}`, n: c.n }))} />
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="between"><h2 style={{ margin: 0 }}>Where the cases are</h2>
          <Link to="/tracker" className="dim" style={{ fontSize: 12 }}>open the progress board →</Link></div>
        <div className="pipeline-strip">
          {data.pipeline.map((s) => (
            <Link to={`/tracker#${s.key}`} key={s.key} className="pipe-stage">
              <div className="stat-label">{s.label}</div>
              <div className="stat-value">{s.count}</div>
              <div className="stat-note">{s.oldest_minutes != null && s.count ? `oldest ${Math.round(s.oldest_minutes)}m in this stage` : ' '}</div>
            </Link>
          ))}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h2>Just arrived</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Received</th><th>Customer</th><th className="num">Amount</th><th>Channel</th>
                <th>Risk</th><th className="num">Risk score</th><th>Alert reasons</th><th>Case</th></tr>
            </thead>
            <tbody>
              {data.recent.map((t) => (
                <tr key={t.id} className={t.case_id ? 'flagged' : ''}>
                  <td className="mono dim">{new Date(t.ingested_at).toLocaleTimeString('en-GB')}</td>
                  <td>{t.display_name || <span className="dim">…{String(t.transaction_ref).slice(-6)}</span>}</td>
                  <td className="num">{nairaShort(t.amount_minor)}{t.direction === 'INBOUND' && <span className="tag"> in</span>}</td>
                  <td className="muted">{say(CHANNEL, t.channel)}</td>
                  <td>{t.risk_level ? <RiskBadge level={t.risk_level} /> : <span className="pill">waiting</span>}</td>
                  <td className="num">{t.score_0_100 ?? '—'}</td>
                  <td className="dim" style={{ fontSize: 11.5 }}>{(t.signals || []).map((s) => say(RULE, s)).join('; ') || '—'}</td>
                  <td>{t.case_id ? <Link to={`/triage?case=${t.case_id}`}>#{t.case_id} →</Link> : <span className="dim">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
