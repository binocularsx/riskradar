import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, nairaShort, when } from '../lib/api'
import { stateName } from '../lib/geo'
import { CHANNEL, OUTCOME, RULE, STAGE, say } from '../lib/words'
import { Banner, RiskBadge } from '../components/ui'
import StateMap, { arc, mix } from '../components/StateMap'

/**
 * Geo survey (D113): one case on the map.
 *
 * Opened from case review when the analyst wants more than the alert itself.
 * It answers, in order: where did the flagged activity happen, and is that
 * where this customer normally is? Who else is tied to it — customers who paid
 * the same accounts or used the same phone — and where are they? Where did the
 * money go? And are there other cases nearby with the same warning signs?
 *
 * The limit is said on screen: destinations are accounts at other banks, so the
 * trail ends at the receiving account. What can be seen is who *else* is
 * paying that account and from where, which is how a collection account behind
 * a mule network shows up.
 */

const PERIODS = [7, 30, 90]

const C = {
  alert: '#dc2626',
  usual: '#2563eb',
  footLo: '#e3ecfb',
  footHi: '#a9c4f2',
  linked: '#7c5cbf',
  money: '#c94704',
  nearby: '#b91c1c',
}

const LAYERS = [
  { key: 'customer', label: 'This customer', colour: C.usual },
  { key: 'linked', label: 'Linked customers', colour: C.linked },
  { key: 'money', label: 'Where the money went', colour: C.money },
  { key: 'nearby', label: 'Similar cases nearby', colour: C.nearby },
]

const GUTTER = 330
const NODE_W = 280
const NODE_H = 58

function Insight({ label, value, note, tone }) {
  return (
    <div className={`geo-insight ${tone || ''}`}>
      <span className="stat-label">{label}</span>
      <span className="geo-insight-value">{value}</span>
      <span className="stat-note">{note}</span>
    </div>
  )
}

function CaseLink({ id, visible }) {
  if (!id) return <span className="dim">No case</span>
  return visible ? <Link to={`/cases/${id}`}>CASE-{id}</Link>
    : <span className="dim" title="Assigned to someone else, so you cannot open it">CASE-{id}</span>
}

function caseResult(state, outcome) {
  if (outcome) return say(OUTCOME, outcome)
  return say(STAGE, state)
}

export default function GeoSurvey() {
  const { id } = useParams()
  const caseId = Number(id)
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [layers, setLayers] = useState({ customer: true, linked: true, money: true, nearby: true })
  const [picked, setPicked] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.caseGeo(caseId, days)
      .then((d) => { if (alive) { setData(d); setPicked((p) => p ?? d.events.find((e) => e.ip_region)?.ip_region ?? null) } })
      .catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [caseId, days])

  const model = useMemo(() => (data ? build(data) : null), [data])
  const toggle = (k) => setLayers((l) => ({ ...l, [k]: !l[k] }))

  return (
    <div className="page ops-dashboard cases-workspace geo-survey">
      <header className="investigation-header">
        <div className="investigation-header-main">
          <div>
            <nav className="investigation-breadcrumb" aria-label="Breadcrumb">
              <Link to="/triage">Cases</Link><span aria-hidden="true">/</span>
              <Link to={`/cases/${caseId}`}>CASE-{caseId}</Link><span aria-hidden="true">/</span><span>Geo survey</span>
            </nav>
            <h1>Geo survey</h1>
            <p>Where this case's activity happened, who else is tied to it, and where the money went.</p>
          </div>
          <div className="ops-head-actions">
            <Link className="ops-action" to={`/cases/${caseId}`}>Back to the case</Link>
          </div>
        </div>
        {data && (
          <div className="investigation-header-meta">
            <div className="investigation-case-label">
              <strong>{data.customer_name || 'Unknown customer'}</strong>
              <RiskBadge level={data.case.risk_level} />
              <span className="pill">{caseResult(data.case.state, data.case.outcome)}</span>
            </div>
            <div className="segmented" role="group" aria-label="Period">
              {PERIODS.map((d) => (
                <button key={d} type="button" className={days === d ? 'on' : ''} aria-pressed={days === d}
                        onClick={() => setDays(d)}>{d} days before the case</button>
              ))}
            </div>
          </div>
        )}
      </header>

      {error && <Banner kind="error">{error}</Banner>}
      {!data && !error && <div className="card"><p className="muted" style={{ margin: 0 }}>Mapping this case…</p></div>}

      {data && model && (
        <>
          <div className="geo-insights">
            <Insight tone="hot" label="Where the alerts happened"
                     value={model.alertStates.length ? model.alertStates.map(stateName).join(', ') : 'No location recorded'}
                     note={model.alertNote} />
            <Insight label="Linked customers"
                     value={data.linked.length ? `${data.linked.length} in ${model.linkedStates.length} state${model.linkedStates.length === 1 ? '' : 's'}` : 'None found'}
                     note={data.linked.length ? `Paid the same accounts${model.deviceLinks ? ' or used the same device' : ''}. ${model.linkedCases === 1 ? '1 of them has a case' : `${model.linkedCases} of them have cases`}${model.linkedFraud ? `, ${model.linkedFraud} confirmed fraud` : ''}.` : 'Nobody else paid these accounts or used these devices in the period.'} />
            <Insight label="Where the money went"
                     value={data.destinations.length ? `${data.destinations.length} account${data.destinations.length === 1 ? '' : 's'} at other banks` : 'No payments out'}
                     note={data.destinations.length ? `${model.sharedAccounts} also paid by other customers here · ${nairaShort(data.flow.out_minor)} sent in the period.` : 'This customer sent nothing in the period.'} />
            <Insight label="Similar cases nearby"
                     value={data.nearby.length ? `${data.nearby.length} case${data.nearby.length === 1 ? '' : 's'}` : 'None'}
                     note={data.nearby.length ? `In ${model.alertStates.map(stateName).join(', ')} within two days${data.nearby_codes.length ? ', sharing a warning sign' : ''}.` : 'No other case alerted in the same state within two days.'} />
          </div>

          <div className="geo-layout">
            <section className="card geo-map-card" aria-label="Map">
              <div className="between wrap" style={{ gap: 10, marginBottom: 10 }}>
                <h2 style={{ margin: 0 }}>Map</h2>
                <div className="geo-layers" role="group" aria-label="Show on the map">
                  {LAYERS.map((l) => (
                    <label key={l.key} className={`geo-layer ${layers[l.key] ? 'on' : ''}`}>
                      <input type="checkbox" checked={layers[l.key]} onChange={() => toggle(l.key)} />
                      <i style={{ background: l.colour }} />{l.label}
                    </label>
                  ))}
                </div>
              </div>
              <SurveyMap data={data} model={model} layers={layers} picked={picked} onPick={setPicked} />
            </section>

            <aside className="card geo-side" aria-label="Selected state">
              <StatePanel code={picked} data={data} model={model} />
            </aside>
          </div>

          <section className="card">
            <h2>The flagged activity</h2>
            <p className="investigation-section-sub">Each alert on this case, where it came from, and where the money was going.</p>
            <div className="table-scroll">
              <table className="rowtable">
                <thead><tr><th>When</th><th className="num">Amount</th><th>Where</th><th>Channel</th><th>Sent to</th><th>Risk</th><th>Warning signs</th></tr></thead>
                <tbody>
                  {data.events.map((e) => (
                    <tr key={e.id}>
                      <td className="dim">{when(e.occurred_at)}</td>
                      <td className="num">{nairaShort(e.amount_minor)}{e.direction === 'INBOUND' && <span className="tag"> in</span>}</td>
                      <td>{e.ip_region ? stateName(e.ip_region) : <span className="dim">Not recorded</span>}
                        {e.new_region && <span className="pill escalate" style={{ marginLeft: 6 }}>New to this customer</span>}</td>
                      <td className="muted">{say(CHANNEL, e.channel)}</td>
                      <td>{e.destination || <span className="dim">—</span>}</td>
                      <td><RiskBadge level={e.risk_level} /></td>
                      <td className="dim" style={{ fontSize: 12 }}>{(e.codes || []).map((c) => say(RULE, c)).join('; ') || 'The risk model on its own'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="card">
            <h2>Where the money went</h2>
            <p className="investigation-section-sub">{data.limits} What the bank can see is who else paid each account, and from where.</p>
            {data.destinations.length ? (
              <div className="table-scroll">
                <table className="rowtable">
                  <thead><tr><th>Account</th><th className="num">Payments from this customer</th><th className="num">Value</th>
                    <th className="num">Other customers paying it</th><th>Where they paid from</th><th>Warnings</th></tr></thead>
                  <tbody>
                    {data.destinations.map((d) => (
                      <tr key={d.label}>
                        <td><strong>{d.label}</strong> <span className="dim">ending {d.ending}</span></td>
                        <td className="num">{d.payments}</td>
                        <td className="num">{nairaShort(d.value_minor)}</td>
                        <td className="num">{d.other_customers}</td>
                        <td>{d.payers_by_state.length
                          ? d.payers_by_state.slice(0, 4).map((p) => <span key={p.ip_region} className="tag" style={{ marginRight: 4 }}>{stateName(p.ip_region)} · {p.customers}</span>)
                          : <span className="dim">Only this customer</span>}</td>
                        <td>
                          {d.lists?.includes('KNOWN_MULE') && <span className="pill escalate">Known mule account</span>}
                          {d.lists?.includes('SANCTIONED') && <span className="pill escalate">On the sanctions list</span>}
                          {d.confirmed_fraud_cases > 0 && <span className="pill escalate">{d.confirmed_fraud_cases} confirmed fraud case{d.confirmed_fraud_cases === 1 ? '' : 's'}</span>}
                          {!d.lists?.length && !d.confirmed_fraud_cases && <span className="dim">None recorded</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="dim">This customer sent no payments in the period.</p>}
            {(data.money_in.length > 0 || data.flow.passed_on_minor > 0) && (
              <p className="geo-readout">
                Money also came in: {data.money_in.map((m) => `${m.credits} credit${m.credits === 1 ? '' : 's'} from bank ${m.bank || 'unknown'} (${nairaShort(m.value_minor)})`).join(', ')}.
                {' '}{nairaShort(data.flow.passed_on_minor)} of what left was sent within a day of money arriving
                {data.flow.out_minor ? ` — ${Math.round((100 * data.flow.passed_on_minor) / data.flow.out_minor)}% of everything sent` : ''}.
              </p>
            )}
          </section>

          <div className="grid cols-2 geo-tables">
            <section className="card">
              <h2>Linked customers</h2>
              <p className="investigation-section-sub">Other customers who paid the same accounts or used the same device in the period.</p>
              {data.linked.length ? (
                <div className="table-scroll">
                  <table className="rowtable">
                    <thead><tr><th>Customer</th><th>Mostly pays from</th><th>Linked through</th><th className="num">Payments</th><th>Their latest case</th></tr></thead>
                    <tbody>
                      {data.linked.map((l, i) => (
                        <tr key={i} className={picked && l.home_region === picked ? 'hot' : ''}>
                          <td>{l.name}</td>
                          <td>{l.home_region ? stateName(l.home_region) : <span className="dim">Not recorded</span>}</td>
                          <td className="muted">{[l.accounts.length ? l.accounts.join(', ') : null, l.via.includes('device') ? 'the same device' : null].filter(Boolean).join(' · ')}</td>
                          <td className="num">{l.payments || '—'}</td>
                          <td><CaseLink id={l.case_id} visible={l.case_visible} />{l.case_id && <span className="dim"> · {caseResult(l.case_state, l.case_outcome)}</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <p className="dim">None in the period.</p>}
            </section>

            <section className="card">
              <h2>Similar cases nearby</h2>
              <p className="investigation-section-sub">Other cases that alerted in the same state within two days of this one{data.nearby_codes.length ? ', with a warning sign in common' : ''}.</p>
              {data.nearby.length ? (
                <div className="table-scroll">
                  <table className="rowtable">
                    <thead><tr><th>Case</th><th>Where</th><th className="num">Alerts</th><th>In common</th><th>Risk</th><th>Stage</th></tr></thead>
                    <tbody>
                      {data.nearby.map((n) => (
                        <tr key={`${n.case_id}-${n.ip_region}`}>
                          <td><CaseLink id={n.case_id} visible={n.case_visible} /></td>
                          <td>{stateName(n.ip_region)}</td>
                          <td className="num">{n.alerts}</td>
                          <td className="dim" style={{ fontSize: 12 }}>{n.shared.length ? n.shared.map((c) => say(RULE, c)).join('; ') : 'Same state and time'}</td>
                          <td><RiskBadge level={n.risk_level} /></td>
                          <td className="muted">{caseResult(n.state, n.outcome)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <p className="dim">None.</p>}
            </section>
          </div>

          <p className="loc-foot">
            Period: {when(data.window.start)} to {when(data.window.end)} — {data.window.days} days before the case opened,
            up to a week after its last alert. {data.note} "Usually pays from" is the state this customer used most in the
            30 days before the case, the same look-back the detector uses.
          </p>
        </>
      )}
    </div>
  )
}

/** Everything the map and the side panel derive from the response, worked out once. */
function build(data) {
  const alertStates = [...new Set(data.events.map((e) => e.ip_region).filter(Boolean))]
  const usual = data.usual_regions[0]?.ip_region || null
  const newOnes = data.events.filter((e) => e.new_region).map((e) => e.ip_region)
  const footprint = Object.fromEntries(data.footprint.filter((f) => f.ip_region).map((f) => [f.ip_region, f]))
  const maxFoot = Math.max(1, ...data.footprint.filter((f) => f.ip_region).map((f) => f.payments))
  const linkedBy = {}
  for (const l of data.linked) if (l.home_region) (linkedBy[l.home_region] ||= []).push(l)
  const nearbyBy = {}
  for (const n of data.nearby) (nearbyBy[n.ip_region] ||= []).push(n)
  const payersBy = {}
  for (const d of data.destinations) for (const p of d.payers_by_state) {
    const r = (payersBy[p.ip_region] ||= { customers: 0, payments: 0, accounts: [] })
    r.customers += p.customers; r.payments += p.payments; r.accounts.push(d.label)
  }
  let alertNote
  if (!alertStates.length) alertNote = 'The flagged payments carry no location.'
  else if (newOnes.length) alertNote = `New to this customer${usual ? ` — they usually pay from ${stateName(usual)}` : ''}.`
  else alertNote = usual && alertStates.includes(usual) ? `The state this customer usually pays from.` : `A state this customer had used before.`
  return {
    alertStates, alertNote, usual, footprint, maxFoot, linkedBy, nearbyBy, payersBy,
    newFrom: newOnes.length && usual && !alertStates.includes(usual) ? usual : null,
    linkedStates: Object.keys(linkedBy),
    linkedCases: data.linked.filter((l) => l.case_id).length,
    linkedFraud: data.linked.filter((l) => l.case_outcome === 'CONFIRMED_FRAUD').length,
    deviceLinks: data.linked.some((l) => l.via.includes('device')),
    sharedAccounts: data.destinations.filter((d) => d.other_customers > 0).length,
  }
}

function SurveyMap({ data, model, layers, picked, onPick }) {
  // Accounts others also paid come first: those are the ones that make a network.
  const sorted = [...data.destinations].sort((a, b) => b.other_customers - a.other_customers || b.value_minor - a.value_minor)
  const shown = [...sorted.filter((d) => d.other_customers > 0), ...sorted.filter((d) => !d.other_customers).slice(0, 2)].slice(0, 6)
  const hidden = data.destinations.length - shown.length
  const maxPayers = Math.max(1, ...shown.flatMap((d) => d.payers_by_state.map((p) => p.customers)))
  const maxLinked = Math.max(1, ...Object.values(model.linkedBy).map((l) => l.length))

  const fill = (code) => {
    if (!layers.customer) {
      if (layers.linked && model.linkedBy[code]) return mix('#ece6f7', '#c9b8ea', model.linkedBy[code].length / maxLinked)
      return null
    }
    if (model.alertStates.includes(code)) return C.alert
    if (code === model.usual) return C.usual
    const f = model.footprint[code]
    if (f) return mix(C.footLo, C.footHi, f.payments / model.maxFoot)
    if (layers.linked && model.linkedBy[code]) return '#ece6f7'
    return null
  }
  const labels = [...new Set([...model.alertStates, model.usual, ...(layers.linked ? model.linkedStates : []),
    ...(layers.money ? Object.keys(model.payersBy) : [])].filter(Boolean))]

  return (
    <StateMap fill={fill} labels={labels} picked={picked} onPick={onPick} gutter={layers.money && shown.length ? GUTTER : 0}
              title={`Map of Nigeria for case ${data.case.id}`}
              footer={<>
                {layers.customer && <>
                  <span className="map-key"><i style={{ background: C.alert }} />Where the alerts happened</span>
                  {model.usual && <span className="map-key"><i style={{ background: C.usual }} />Usually pays from</span>}
                  <span className="map-key"><i style={{ background: C.footHi }} />Other states this customer paid from</span>
                </>}
                {layers.linked && <span className="map-key"><i className="dot" style={{ background: C.linked }} />Linked customers, by the state they mostly pay from</span>}
                {layers.money && <span className="map-key"><i className="line" style={{ background: C.money }} />This customer's payments · <i className="line" style={{ background: C.linked }} />other customers paying the same account</span>}
                {layers.nearby && <span className="map-key"><i className="dot" style={{ background: '#fff', border: `2px solid ${C.nearby}` }} />Similar cases nearby</span>}
              </>}>
      {({ at, mapWidth }) => {
        const nodeAt = (i) => [mapWidth + 30, 40 + i * ((812 - 80) / Math.max(shown.length, 1))]
        return (
          <>
            <defs>
              <marker id="geo-head-money" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={C.money} />
              </marker>
              <marker id="geo-head-alert" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={C.alert} />
              </marker>
            </defs>

            {layers.money && shown.map((d, i) => {
              const [nx, ny] = nodeAt(i)
              const target = [nx, ny + NODE_H / 2]
              return (
                <g key={d.label}>
                  {d.payers_by_state.map((p) => at(p.ip_region) && (
                    <path key={p.ip_region} d={arc(at(p.ip_region), target, { bend: 0.12, stop: 2 }).d} fill="none"
                          stroke={C.linked} strokeOpacity={0.45} strokeWidth={1 + (p.customers / maxPayers) * 5}>
                      <title>{`${p.customers} other customer${p.customers === 1 ? '' : 's'} in ${stateName(p.ip_region)} paid ${d.label}`}</title>
                    </path>
                  ))}
                  {d.from_states.map((s) => at(s) && (
                    <path key={`me-${s}`} d={arc(at(s), target, { bend: -0.1, stop: 4 }).d} fill="none" stroke={C.money}
                          strokeWidth={2.2} strokeOpacity={0.75} markerEnd="url(#geo-head-money)">
                      <title>{`This customer paid ${d.label} from ${stateName(s)}`}</title>
                    </path>
                  ))}
                  <g transform={`translate(${nx},${ny})`}>
                    <rect width={NODE_W} height={NODE_H} rx={10} className={`geo-node ${d.other_customers ? 'shared' : ''}`} />
                    <text x={14} y={24} className="geo-node-title">{d.label} · ending {d.ending}</text>
                    <text x={14} y={44} className="geo-node-sub">
                      {d.other_customers ? `${d.other_customers} other customer${d.other_customers === 1 ? '' : 's'} paid it` : 'Only this customer'}
                      {d.confirmed_fraud_cases ? ' · confirmed fraud' : d.lists?.length ? ' · on a watch list' : ''}
                    </text>
                  </g>
                </g>
              )
            })}
            {layers.money && shown.length > 0 && (
              <>
                <text x={mapWidth + 30} y={24} className="geo-gutter-title">Accounts at other banks</text>
                {hidden > 0 && <text x={mapWidth + 30} y={800} className="geo-node-sub">{hidden} more in the table below</text>}
              </>
            )}

            {layers.customer && model.newFrom && model.alertStates.map((s) => at(s) && at(model.newFrom) && (
              <path key={`move-${s}`} d={arc(at(model.newFrom), at(s), { bend: 0.25, stop: 12 }).d} fill="none"
                    stroke={C.alert} strokeWidth={3} strokeDasharray="7 6" markerEnd="url(#geo-head-alert)">
                <title>{`Usually pays from ${stateName(model.newFrom)}; the alerts came from ${stateName(s)}`}</title>
              </path>
            ))}

            {layers.customer && model.usual && at(model.usual) && !model.alertStates.includes(model.usual) && (
              <g transform={`translate(${at(model.usual)[0]},${at(model.usual)[1] - 30})`} pointerEvents="none">
                <circle r={13} fill={C.usual} stroke="#fff" strokeWidth={3} />
                <text textAnchor="middle" y={5} className="geo-count">U</text>
                <title>Usually pays from here</title>
              </g>
            )}
            {layers.customer && model.alertStates.map((code) => at(code) && (
              <g key={`a-${code}`} transform={`translate(${at(code)[0]},${at(code)[1] - 30})`} pointerEvents="none">
                <circle r={15} fill="#7f1d1d" stroke="#fff" strokeWidth={3} />
                <text textAnchor="middle" y={5} className="geo-count">{data.events.filter((e) => e.ip_region === code).length}</text>
              </g>
            ))}

            {layers.linked && Object.entries(model.linkedBy).map(([code, list]) => {
              const p = at(code); if (!p) return null
              const r = 9 + Math.sqrt(list.length / maxLinked) * 15
              return (
                <g key={`l-${code}`} transform={`translate(${p[0] + 34},${p[1] - 12})`} pointerEvents="none">
                  <circle r={r} fill={C.linked} fillOpacity={0.9} stroke="#fff" strokeWidth={2} />
                  <text textAnchor="middle" y={4.5} className="geo-count">{list.length}</text>
                </g>
              )
            })}

            {layers.nearby && Object.entries(model.nearbyBy).map(([code, list]) => {
              const p = at(code); if (!p) return null
              return (
                <g key={`n-${code}`} transform={`translate(${p[0] - 50},${p[1] - 4})`} pointerEvents="none">
                  <circle r={14} fill="#fff" stroke={C.nearby} strokeWidth={2.5} />
                  <text textAnchor="middle" y={4.5} className="geo-count nearby">{list.length}</text>
                </g>
              )
            })}
          </>
        )
      }}
    </StateMap>
  )
}

function StatePanel({ code, data, model }) {
  if (!code) return <p className="dim" style={{ margin: 0 }}>Select a coloured state on the map to see what happened there.</p>
  const f = model.footprint[code]
  const linked = model.linkedBy[code] || []
  const nearby = model.nearbyBy[code] || []
  const payers = model.payersBy[code]
  const alerts = data.events.filter((e) => e.ip_region === code)
  return (
    <>
      <div className="ops-eyebrow">Selected state</div>
      <h2 style={{ margin: '4px 0 12px' }}>{stateName(code)}</h2>
      <dl className="geo-facts">
        <div><dt>This customer</dt><dd>{f ? `${f.payments.toLocaleString()} payment${f.payments === 1 ? '' : 's'}, ${nairaShort(f.value_out_minor)} sent` : 'No payments from here'}
          {code === model.usual && <span className="pill" style={{ marginLeft: 6 }}>Usual state</span>}</dd></div>
        <div><dt>Alerts on this case</dt><dd>{alerts.length ? `${alerts.length}${alerts.some((a) => a.new_region) ? ', new to this customer' : ''}` : 'None'}</dd></div>
        <div><dt>Linked customers here</dt><dd>{linked.length || 'None'}</dd></div>
        <div><dt>Others paying the same accounts from here</dt><dd>{payers ? `${payers.customers} customer${payers.customers === 1 ? '' : 's'} · ${[...new Set(payers.accounts)].join(', ')}` : 'None'}</dd></div>
        <div><dt>Similar cases here</dt><dd>{nearby.length || 'None'}</dd></div>
      </dl>
      {linked.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Linked customers in {stateName(code)}</h3>
          <ul className="geo-list">
            {linked.slice(0, 8).map((l, i) => (
              <li key={i}><span>{l.name}</span><span className="dim">{l.accounts.join(', ') || 'same device'}{l.case_id ? <> · <CaseLink id={l.case_id} visible={l.case_visible} /></> : ''}</span></li>
            ))}
          </ul>
        </>
      )}
      {nearby.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Similar cases in {stateName(code)}</h3>
          <ul className="geo-list">
            {nearby.slice(0, 8).map((n) => (
              <li key={n.case_id}><CaseLink id={n.case_id} visible={n.case_visible} /><span className="dim">{n.alerts} alert{n.alerts === 1 ? '' : 's'} · {caseResult(n.state, n.outcome)}</span></li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}
