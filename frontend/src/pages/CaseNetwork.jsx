import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, nairaShort, when } from '../lib/api'
import { stateName } from '../lib/geo'
import { CHANNEL, OUTCOME, RULE, STAGE, say } from '../lib/words'
import { Banner, RiskBadge } from '../components/ui'
import NetworkStage, { KIND, Ranking } from '../components/NetworkStage'

/**
 * Case network (D115): the connections view and the geo survey as one tool.
 *
 * Opened from case review when the analyst wants more than the alert itself.
 * It answers, in order: who and what is this case tied to — accounts paid,
 * devices used, other customers who paid the same accounts or used the same
 * phone, similar cases nearby — where did each of them happen, and in what
 * order did the links appear? One canvas shows it as a network or on the map
 * (NetworkStage); the list beside it says who to look at first and why; the
 * tables below hold the same facts for reading line by line.
 *
 * The limit is said on screen: destinations are accounts at other banks, so the
 * trail ends at the receiving account. What can be seen is who *else* is
 * paying that account and from where, which is how a collection account behind
 * a mule network shows up.
 */

const PERIODS = [7, 30, 90]

const LAYERS = [{ key: 'person' }, { key: 'account' }, { key: 'device' }, { key: 'case' }]

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

export default function CaseNetwork() {
  const { id } = useParams()
  const caseId = Number(id)
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  // Usual devices start hidden: a customer's own phone is not news. New or shared ones always show.
  const [layers, setLayers] = useState({ person: true, account: true, device: false, case: true })
  const [view, setView] = useState('network')
  const [selected, setSelected] = useState(null)
  const [cursor, setCursor] = useState(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.caseGeo(caseId, days)
      .then((d) => { if (alive) { setData(d); setCursor(null) } })
      .catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [caseId, days])

  const model = useMemo(() => (data ? build(data) : null), [data])
  const toggle = (k) => setLayers((l) => ({ ...l, [k]: !l[k] }))

  return (
    <div className="page ops-dashboard cases-workspace geo-survey case-network">
      <header className="investigation-header">
        <div className="investigation-header-main">
          <div>
            <nav className="investigation-breadcrumb" aria-label="Breadcrumb">
              <Link to="/triage">Cases</Link><span aria-hidden="true">/</span>
              <Link to={`/cases/${caseId}`}>CASE-{caseId}</Link><span aria-hidden="true">/</span><span>Case network</span>
            </nav>
            <h1>Case network</h1>
            <p>Who and what this case is tied to, where it happened, and how it built up over time.</p>
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

          <section className="ns-stage" aria-label="Case network">
            <div className="ns-filters" role="group" aria-label="Show">
              <span className="ns-filters-label">Show</span>
              <span className="ns-chip on fixed" title="Always shown">
                <i className="ns-swatch customer" style={{ '--c': KIND.customer.colour }} aria-hidden="true" />
                {data.customer_name || 'This customer'}
              </span>
              {LAYERS.map((l) => (
                <label key={l.key} className={`ns-chip ${layers[l.key] ? 'on' : ''}`}>
                  <input type="checkbox" checked={layers[l.key]} onChange={() => toggle(l.key)} />
                  <i className={`ns-swatch ${l.key}`} style={{ '--c': KIND[l.key].colour }} aria-hidden="true" />
                  {l.key === 'device' ? 'Usual devices' : KIND[l.key].label}
                  <span className="ns-chip-n">{l.key === 'device'
                    ? `${data.graph.nodes.filter((n) => n.kind === 'device' && n.routine).length}${data.graph.nodes.some((n) => n.kind === 'device' && !n.routine) ? ` · ${data.graph.nodes.filter((n) => n.kind === 'device' && !n.routine).length} new or shared always shown` : ''}`
                    : data.graph.nodes.filter((n) => n.kind === l.key).length}</span>
                </label>
              ))}
            </div>
            <div className="ns-body">
              <NetworkStage graph={data.graph} view={view} onView={setView} layers={layers}
                            selected={selected} onSelect={setSelected} cursor={cursor} onCursor={setCursor} query={query} />
              <Ranking graph={data.graph} layers={layers} selected={selected} onSelect={setSelected}
                       query={query} onQuery={setQuery} />
            </div>
          </section>

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
                        <tr key={i}>
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

