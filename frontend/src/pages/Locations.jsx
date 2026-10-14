import { useEffect, useMemo, useState } from 'react'
import {
  Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

import { api, ago, nairaShort, when } from '../lib/api'
import { CHART, axis, grid, legend, tooltip } from '../lib/chart'
import { stateName } from '../lib/geo'
import { CHANNEL, RULE, say } from '../lib/words'
import { Banner } from '../components/ui'
import StateMap, { arc, mix } from '../components/StateMap'

/**
 * Location analysis (D113): the whole bank by state, and how that changes over time.
 *
 * This is the administrator's view of what the system is seeing geographically
 * — which states alert more than the bank as a whole, whether customers are
 * turning up there from somewhere else, and whether that is new or has been
 * building for weeks. It holds no case and no customer, because the
 * administrator may see neither (D12b); the analysts and leads get the same
 * picture for one case at a time, as the Geo survey inside case review.
 *
 * Every number comes from `transactions.ip_region`, the state the session
 * appeared to come from, which the page says plainly.
 */

const PERIODS = [
  { hours: 24, label: '24 hours' },
  { hours: 168, label: '7 days' },
  { hours: 720, label: '30 days' },
  { hours: 2160, label: '90 days' },
]

// What the state colour shows. The first compares states fairly; the rest are
// raw totals, which mostly show where people live and are labelled as such.
const MEASURES = [
  { key: 'lift', label: 'Alert rate against the bank' },
  { key: 'alerts', label: 'Alerts' },
  { key: 'new_region', label: 'New-state payments' },
  { key: 'flagged_value_minor', label: 'Value flagged' },
  { key: 'transactions', label: 'Payments' },
]

const CHANNELS = ['MOBILE_APP', 'WEB', 'USSD', 'POS', 'ATM', 'AGENT', 'BRANCH']

const pct = (v, digits = 1) => (v == null ? '—' : `${(Number(v) * 100).toFixed(digits)}%`)
const times = (v) => (v == null ? '—' : `${Number(v).toFixed(1)}×`)
const fmt = (n) => Number(n || 0).toLocaleString()

// Diverging: teal below the bank's rate, grey at it, red above. Doubling or
// halving is the full swing, because "twice the bank's rate" is the comparison
// people actually make.
const LOW = '#0e7490'
const MID = '#b8bec7'
const HIGH = '#dc2626'
function liftColour(lift) {
  if (lift == null) return MID
  const t = Math.max(-1, Math.min(1, Math.log2(Math.max(lift, 0.01))))
  return t < 0 ? mix(MID, LOW, -t) : mix(MID, HIGH, t)
}
const SEQ_LO = '#fde8d7'
const SEQ_HI = '#b83f04'

function Insight({ label, value, note, tone, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag className={`loc-insight ${tone || ''}`} onClick={onClick} type={onClick ? 'button' : undefined}>
      <span className="stat-label">{label}</span>
      <span className="loc-insight-value">{value}</span>
      <span className="stat-note">{note}</span>
    </Tag>
  )
}

const COLUMNS = [
  { key: 'name', label: 'State', num: false },
  { key: 'transactions', label: 'Payments' },
  { key: 'alerts', label: 'Alerts', title: 'Alerts, and the share of payments that alerted' },
  { key: 'lift', label: 'Vs bank' },
  { key: 'new_region_share_of_alerts', label: 'New-state share', title: 'Share of alerts from customers new to this state' },
]

function StateTable({ states, picked, onPick, colour }) {
  const [sort, setSort] = useState({ key: 'lift', desc: true })
  const rows = [...states].map((s) => ({ ...s, name: stateName(s.ip_region) })).sort((a, b) => {
    const [x, y] = [a[sort.key], b[sort.key]]
    const c = typeof x === 'string' ? x.localeCompare(y) : Number(x ?? -1) - Number(y ?? -1)
    return sort.desc ? -c : c
  })
  const head = (c) => (
    <th key={c.key} className={c.num === false ? '' : 'num'} title={c.title}
        aria-sort={sort.key === c.key ? (sort.desc ? 'descending' : 'ascending') : 'none'}>
      <button type="button" className="loc-sort" onClick={() => setSort((s) => ({ key: c.key, desc: s.key === c.key ? !s.desc : c.num !== false }))}>
        {c.label}{sort.key === c.key ? (sort.desc ? ' ↓' : ' ↑') : ''}
      </button>
    </th>
  )
  return (
    <div className="table-scroll">
      <table className="rowtable loc-table">
        <thead><tr>{COLUMNS.map(head)}</tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.ip_region} className={picked === s.ip_region ? 'hot' : ''} onClick={() => onPick(s.ip_region)}>
              <td><span className="loc-swatch" style={{ background: colour(s.ip_region) }} />{s.name}</td>
              <td className="num">{fmt(s.transactions)}</td>
              <td className="num">{s.alerts} <span className="dim">· {pct(s.alert_rate, 2)}</span></td>
              <td className="num"><strong>{times(s.lift)}</strong></td>
              <td className="num">{s.alerts ? pct(s.new_region_share_of_alerts, 0) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function HBars({ rows, empty }) {
  const max = Math.max(1, ...rows.map((r) => r.n))
  if (!rows.length) return <p className="dim" style={{ margin: 0, fontSize: 12.5 }}>{empty}</p>
  return (
    <div className="hbars">
      {rows.map((r) => (
        <div className="hbar" key={r.key}>
          <span className="hbar-label" title={r.label}>{r.label}</span>
          <span className="hbar-track"><span className="hbar-fill" style={{ width: `${(r.n / max) * 100}%`, background: r.colour || 'var(--accent)' }} /></span>
          <span className="num hbar-n">{r.n}{r.extra ? <span className="dim"> · {r.extra}</span> : null}</span>
        </div>
      ))}
    </div>
  )
}

function TrendChart({ data, name }) {
  const rows = data.map((d) => ({
    ...d,
    label: new Date(`${d.day}T12:00:00`).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }),
    rate: +(d.alert_rate * 100).toFixed(3),
    bank: +(d.bank_alert_rate * 100).toFixed(3),
  }))
  return (
    <ResponsiveContainer width="100%" height={250}>
      <ComposedChart data={rows} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
        <CartesianGrid {...grid} />
        <XAxis dataKey="label" {...axis} minTickGap={24} />
        <YAxis yAxisId="n" {...axis} allowDecimals={false} width={36} />
        <YAxis yAxisId="r" orientation="right" {...axis} width={46} tickFormatter={(v) => `${v}%`} />
        <Tooltip {...tooltip} formatter={(v, n) => (n.includes('rate') ? `${v}%` : v)} />
        <Legend {...legend} />
        <Bar yAxisId="n" dataKey="alerts" name={`Alerts in ${name}`} fill={CHART.orangeFill} stroke={CHART.orange} strokeWidth={0.6} isAnimationActive={false} />
        <Line yAxisId="r" dataKey="rate" name={`${name} alert rate`} stroke={HIGH} strokeWidth={2.2} dot={false} isAnimationActive={false} />
        <Line yAxisId="r" dataKey="bank" name="Bank alert rate" stroke="#6b7280" strokeDasharray="5 4" strokeWidth={1.8} dot={false} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

function HourChart({ data }) {
  const rows = data.map((h) => ({ ...h, label: `${String(h.hour).padStart(2, '0')}:00` }))
  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={rows} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
        <CartesianGrid {...grid} />
        <XAxis dataKey="label" {...axis} interval={2} />
        <YAxis yAxisId="tx" {...axis} allowDecimals={false} width={48} />
        <YAxis yAxisId="al" orientation="right" {...axis} allowDecimals={false} width={30} />
        <Tooltip {...tooltip} />
        <Legend {...legend} />
        <Bar yAxisId="tx" dataKey="transactions" name="Payments" fill={CHART.blueFill} stroke={CHART.blue} strokeWidth={0.6} radius={[3, 3, 0, 0]} isAnimationActive={false} />
        <Line yAxisId="al" dataKey="alerts" name="Alerts (right scale)" stroke={CHART.orange} strokeWidth={2.2} dot={{ r: 2.5 }} type="monotone" isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

function StateDetail({ region, summary, hours, channel, moves, national, periodLabel }) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setDetail(null); setError(null)
    api.location(region, hours, channel)
      .then((d) => alive && setDetail(d))
      .catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [region, hours, channel])

  const name = stateName(region)
  const incoming = (moves || []).filter((m) => m.to_region === region).slice(0, 6)
  const outgoing = (moves || []).filter((m) => m.from_region === region && m.alerts > 0).slice(0, 6)

  // Is the state's rate rising? Compare the later half of the period with the earlier half.
  const trend = useMemo(() => {
    const days = detail?.by_day || []
    if (days.length < 6) return null
    const half = Math.floor(days.length / 2)
    const sum = (xs, k) => xs.reduce((s, d) => s + d[k], 0)
    const [a, b] = [days.slice(0, half), days.slice(half)]
    const ra = sum(a, 'alerts') / Math.max(1, sum(a, 'transactions'))
    const rb = sum(b, 'alerts') / Math.max(1, sum(b, 'transactions'))
    if (!ra && !rb) return null
    return { before: ra, after: rb, change: ra ? rb / ra : null }
  }, [detail])

  return (
    <section className="card loc-detail" aria-labelledby="loc-detail-title">
      <div className="between wrap" style={{ gap: 10 }}>
        <div>
          <div className="ops-eyebrow">Selected state</div>
          <h2 id="loc-detail-title" style={{ margin: '4px 0 0' }}>{name}</h2>
        </div>
        {summary && (
          <div className="loc-chips">
            <span className="tag">{fmt(summary.transactions)} payments</span>
            <span className="tag">{fmt(summary.customers)} customers</span>
            <span className="tag">{summary.alerts} alerts · {summary.serious_alerts} high or critical</span>
            <span className="tag">{nairaShort(summary.flagged_value_minor)} flagged</span>
            <span className="tag">{fmt(summary.declined)} declined or failed</span>
          </div>
        )}
      </div>

      {summary && summary.alerts > 0 && (
        <p className="loc-readout">
          Over the last {periodLabel}, {name} alerted at <b>{times(summary.lift)}</b> the bank's rate.{' '}
          {summary.new_region_alerts > 0
            ? <><b>{summary.new_region_alerts} of its {summary.alerts} alerts</b> ({pct(summary.new_region_share_of_alerts, 0)}) came from customers paying from {name} for the first time in 30 days.</>
            : <>None of its alerts came from customers new to {name}.</>}
          {incoming[0] && <> Most of those customers usually pay from <b>{stateName(incoming[0].from_region)}</b>.</>}
          {trend?.change != null && Math.abs(trend.change - 1) >= 0.25 && (
            <> Its alert rate in the second half of the period was <b>{trend.change > 1 ? `${trend.change.toFixed(1)}× higher` : `${(1 / trend.change).toFixed(1)}× lower`}</b> than in the first.</>
          )}
        </p>
      )}

      {error && <Banner kind="error">{error}</Banner>}
      {!detail && !error && <p className="muted">Loading {name}…</p>}

      {detail && (
        <div className="loc-detail-grid">
          <div className="loc-panel loc-panel-wide">
            <h3>Over time</h3>
            <p className="loc-help">Alerts per day in {name} (bars), and its alert rate against the whole bank's (lines, right scale).</p>
            {detail.by_day.length > 1 ? <TrendChart data={detail.by_day} name={name} />
              : <p className="dim" style={{ margin: 0, fontSize: 12.5 }}>Pick a period of 7 days or more to see a trend.</p>}
          </div>
          <div className="loc-panel loc-panel-wide">
            <h3>Time of day</h3>
            <p className="loc-help">By hour, Nigerian time, across the whole period. Alerts are on the right-hand scale.</p>
            <HourChart data={detail.by_hour} />
          </div>
          <div className="loc-panel">
            <h3>Why alerts were raised here</h3>
            <HBars empty="No alerts in this state for this period."
                   rows={detail.reasons.map((r) => ({ key: r.code, label: say(RULE, r.code), n: r.n,
                     colour: r.power === 'SUPPRESS' ? 'var(--low)' : 'var(--high)' }))} />
          </div>
          <div className="loc-panel">
            <h3>Channels</h3>
            <HBars empty="No payments."
                   rows={detail.by_channel.map((c) => ({ key: c.channel, label: say(CHANNEL, c.channel), n: c.alerts,
                     extra: `${pct(c.transactions ? c.alerts / c.transactions : 0, 2)} of ${fmt(c.transactions)}` }))} />
            <p className="loc-help" style={{ marginTop: 8 }}>Bars are alerts; the figure after them is the share of that channel's payments that alerted.</p>
          </div>
          <div className="loc-panel">
            <h3>Customers new to {name} usually pay from</h3>
            {incoming.length ? (
              <HBars empty="" rows={incoming.map((m) => ({ key: m.from_region, label: stateName(m.from_region), n: m.customers,
                extra: `${m.alerts} alerts, ${pct(m.alert_rate, 0)} of their payments`, colour: CHART.orange }))} />
            ) : <p className="dim" style={{ margin: 0, fontSize: 12.5 }}>{moves ? `Nobody arrived in ${name} from another state in this period.` : 'Loading…'}</p>}
            {outgoing.length > 0 && <p className="loc-help" style={{ marginTop: 8 }}>
              Customers who usually pay from {name} raised alerts elsewhere: {outgoing.map((m) => `${stateName(m.to_region)} (${m.alerts})`).join(', ')}.</p>}
            <p className="loc-help" style={{ marginTop: 8 }}>Bars are customers. Bank-wide, {pct(national.new_region_alert_rate)} of new-state payments alert.</p>
          </div>
        </div>
      )}
    </section>
  )
}

export default function Locations() {
  const [hours, setHours] = useState(168)
  const [channel, setChannel] = useState('')
  const [measure, setMeasure] = useState('lift')
  const [showMoves, setShowMoves] = useState(true)
  const [picked, setPicked] = useState(null)
  const [data, setData] = useState(null)
  const [moves, setMoves] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setMoves(null); setError(null)
    api.locations(hours, channel)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message))
    api.locationMoves(hours, channel)
      .then((d) => alive && setMoves(d.moves))
      .catch(() => alive && setMoves([]))
    return () => { alive = false }
  }, [hours, channel])

  // Open on the state most worth a look, so the detail is never an empty box:
  // the highest alert rate among states with enough alerts to mean something.
  useEffect(() => {
    if (!data || (picked && data.states.some((s) => s.ip_region === picked))) return
    const ranked = [...data.states].filter((s) => s.alerts >= 5).sort((a, b) => (b.lift ?? 0) - (a.lift ?? 0))
    setPicked((ranked[0] || data.states[0])?.ip_region ?? null)
  }, [data, picked])

  const n = data?.national
  const states = data?.states || []
  const byCode = Object.fromEntries(states.map((s) => [s.ip_region, s]))
  const top = [...states].filter((s) => s.alerts >= 5).sort((a, b) => (b.lift ?? 0) - (a.lift ?? 0))[0]
  const strongest = (moves || []).find((m) => m.alerts > 0)
  const newLift = n && n.usual_region_alert_rate ? n.new_region_alert_rate / n.usual_region_alert_rate : null
  const periodLabel = PERIODS.find((p) => p.hours === hours)?.label
  const maxOf = Math.max(1, ...states.map((s) => Number(s[measure]) || 0))
  const colour = (code) => {
    const s = byCode[code]
    if (!s) return null
    if (measure === 'lift') return liftColour(s.lift)
    return mix(SEQ_LO, SEQ_HI, Math.sqrt((Number(s[measure]) || 0) / maxOf))
  }

  const arrows = (moves || []).filter((m) => m.alerts > 0 && (!picked || m.to_region === picked || m.from_region === picked)).slice(0, 8)
  const maxArrow = Math.max(1, ...arrows.map((m) => m.alerts))

  return (
    <div className="page locations">
      <div className="page-head ops-page-head">
        <div>
          <div className="ops-eyebrow">Analysis</div>
          <h1>Location analysis</h1>
          <p className="page-sub">
            What the system is seeing across Nigeria: which states alert more than the bank as a whole, where customers
            are turning up from, and how that has changed over time. Totals only — no cases or customers.
          </p>
        </div>
      </div>

      <div className="loc-controls card" role="group" aria-label="Filters">
        <div className="loc-control">
          <span className="loc-control-label" id="loc-period">Period</span>
          <div className="segmented" aria-labelledby="loc-period">
            {PERIODS.map((p) => (
              <button key={p.hours} type="button" className={hours === p.hours ? 'on' : ''} aria-pressed={hours === p.hours}
                      onClick={() => setHours(p.hours)}>{p.label}</button>
            ))}
          </div>
        </div>
        <div className="loc-control">
          <label className="loc-control-label" htmlFor="loc-channel">Channel</label>
          <select id="loc-channel" value={channel} onChange={(e) => setChannel(e.target.value)}>
            <option value="">All channels</option>
            {CHANNELS.map((c) => <option key={c} value={c}>{say(CHANNEL, c)}</option>)}
          </select>
        </div>
        <div className="loc-control">
          <label className="loc-control-label" htmlFor="loc-measure">Colour states by</label>
          <select id="loc-measure" value={measure} onChange={(e) => setMeasure(e.target.value)}>
            {MEASURES.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
          </select>
        </div>
        <label className="loc-toggle">
          <input type="checkbox" checked={showMoves} onChange={(e) => setShowMoves(e.target.checked)} />
          Show where customers came from
        </label>
      </div>

      {error && <Banner kind="error">{error}</Banner>}
      {!data && !error && <div className="card"><p className="muted" style={{ margin: 0 }}>Loading location figures… the 30- and 90-day views take several seconds the first time.</p></div>}

      {data && n.transactions === 0 && (
        <Banner kind="warn">
          No payments arrived in the last {periodLabel}. The most recent payment the system received was{' '}
          {data.last_activity_at ? `${ago(data.last_activity_at)} (${when(data.last_activity_at)})` : 'never'}.{' '}
          {hours < 2160 && <button type="button" className="linkish" onClick={() => setHours(hours < 168 ? 168 : hours < 720 ? 720 : 2160)}>
            Show a longer period
          </button>}
        </Banner>
      )}

      {data && n.transactions > 0 && (
        <>
          <div className="loc-insights">
            <Insight label="Paying from a state new to the customer"
                     value={newLift ? `${newLift.toFixed(0)}× more likely to alert` : '—'}
                     tone="hot"
                     note={`${pct(n.new_region_alert_rate)} of ${fmt(n.new_region)} such payments alerted, against ${pct(n.usual_region_alert_rate, 2)} of all others.`} />
            <Insight label="Highest alert rate"
                     value={top ? `${stateName(top.ip_region)} · ${times(top.lift)}` : '—'}
                     note={top ? `${top.alerts} alerts from ${fmt(top.transactions)} payments, against the bank's ${pct(n.alert_rate, 2)}.` : 'Not enough alerts to compare states.'}
                     onClick={top ? () => setPicked(top.ip_region) : undefined} />
            <Insight label="Strongest movement"
                     value={strongest ? `${stateName(strongest.from_region)} → ${stateName(strongest.to_region)}` : (moves ? 'None with alerts' : 'Loading…')}
                     note={strongest ? `${strongest.customers} customers, ${strongest.alerts} alerts: ${pct(strongest.alert_rate, 0)} of their payments there alerted.` : 'Customers who usually pay from one state, paying from another.'}
                     onClick={strongest ? () => setPicked(strongest.to_region) : undefined} />
            <Insight label={`Alerts in the last ${periodLabel}`}
                     value={fmt(n.alerts)}
                     note={`From ${fmt(n.transactions)} outgoing payments in ${states.length} states${channel ? `, ${say(CHANNEL, channel)} only` : ''}.`} />
          </div>

          <div className="loc-grid">
            <section className="card loc-map-card" aria-label="Map">
              <div className="between wrap" style={{ marginBottom: 8 }}>
                <h2 style={{ margin: 0 }}>Map</h2>
                <span className="loc-help" style={{ margin: 0 }}>Select a coloured state to see its detail.</span>
              </div>
              <StateMap fill={colour} labels={states.map((s) => s.ip_region)} picked={picked} onPick={setPicked}
                        title={`Map of Nigeria coloured by ${MEASURES.find((m) => m.key === measure).label.toLowerCase()}`}
                        footer={<>
                          {measure === 'lift' ? (
                            <span className="map-key">
                              <span className="loc-key-ramp" aria-hidden="true">{[0.5, 0.71, 1, 1.41, 2].map((v) => <i key={v} style={{ background: liftColour(v) }} />)}</span>
                              Alert rate against the bank's: <b>half</b> → <b>same</b> → <b>double</b> or more
                            </span>
                          ) : (
                            <span className="map-key">
                              <span className="loc-key-ramp" aria-hidden="true">{[0, 0.25, 0.5, 0.75, 1].map((v) => <i key={v} style={{ background: mix(SEQ_LO, SEQ_HI, v) }} />)}</span>
                              {MEASURES.find((m) => m.key === measure).label}: least → most. Raw totals mostly follow where customers are.
                            </span>
                          )}
                          {showMoves && <span className="map-key"><i className="line" style={{ background: CHART.orange }} />Customers paying from a state new to them, drawn from where they usually pay; the number is alerts</span>}
                          <span className="map-key"><i style={{ background: '#e6e8eb' }} />No activity recorded</span>
                        </>}>
                {({ at }) => showMoves && (
                  <>
                    <defs>
                      <marker id="loc-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                        <path d="M0,0 L10,5 L0,10 z" fill={CHART.orange} />
                      </marker>
                    </defs>
                    {arrows.map((m) => {
                      const [a, b] = [at(m.from_region), at(m.to_region)]
                      if (!a || !b) return null
                      const { d, mid } = arc(a, b, { bend: 0.24, stop: 12 })
                      return (
                        <g key={`${m.from_region}-${m.to_region}`}>
                          <title>{`${stateName(m.from_region)} → ${stateName(m.to_region)}: ${m.customers} customers, ${m.transactions} payments, ${m.alerts} alerts`}</title>
                          <path d={d} fill="none" stroke={CHART.orange} strokeOpacity={0.85} strokeLinecap="round"
                                strokeWidth={2 + (m.alerts / maxArrow) * 6} markerEnd="url(#loc-head)" />
                          <g transform={`translate(${mid[0]},${mid[1]})`}>
                            <rect x={-14} y={-11} width={28} height={21} rx={10} className="loc-move-badge" />
                            <text textAnchor="middle" y={5} className="loc-move-n">{m.alerts}</text>
                          </g>
                        </g>
                      )
                    })}
                  </>
                )}
              </StateMap>
            </section>

            <section className="card loc-rank" aria-label="States compared">
              <h2 style={{ marginBottom: 4 }}>States compared</h2>
              <p className="loc-help">Select a column to sort. "Vs bank" is the state's alert rate divided by the bank's: 2.0× means twice as likely to alert. "New-state share" is how many of its alerts came from customers paying from there for the first time in 30 days.</p>
              <StateTable states={states} picked={picked} onPick={setPicked} colour={colour} />
              <h3 style={{ marginTop: 18 }}>Movements with the most alerts</h3>
              {moves == null ? <p className="muted" style={{ fontSize: 12.5 }}>Working out where customers came from…</p> : (
                <HBars empty="No customers paid from a state new to them in this period."
                       rows={moves.filter((m) => m.alerts > 0).slice(0, 6).map((m) => ({
                         key: `${m.from_region}-${m.to_region}`,
                         label: `${stateName(m.from_region)} → ${stateName(m.to_region)}`,
                         n: m.alerts, extra: `${m.customers} customers`, colour: CHART.orange,
                       }))} />
              )}
            </section>
          </div>

          {picked && <StateDetail key={picked} region={picked} summary={byCode[picked]} hours={hours}
                                  channel={channel} moves={moves} national={n} periodLabel={periodLabel} />}

          <p className="loc-foot">
            {data.note} A state new to the customer means they made no located payment there in the 30 days before,
            the same test the detector uses. Incoming credits are left out. Figures as of {when(data.computed_at)}.
          </p>
        </>
      )}
    </div>
  )
}
