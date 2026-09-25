import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, nairaShort, ago, when } from '../lib/api'

/**
 * Who else is connected to this case (D84).
 *
 * The customer sits in the middle; the destinations they paid and the devices
 * they used sit around them; other customers who paid the same destinations or
 * used the same devices sit on the outside, next to what they share. Red means
 * something is already known against it: a confirmed fraud case, a list entry,
 * or several customers paying a destination in the same day.
 *
 * Beside the picture, every identifier gets a reputation line in words, because
 * a graph shows that a link exists and the line says whether it matters.
 */

const W = 560
const H = 420
const CX = W / 2
const CY = H / 2

function polar(r, angle) {
  return [CX + r * Math.cos(angle), CY + r * Math.sin(angle)]
}

function short(token) {
  return token ? `…${String(token).slice(-6)}` : '—'
}

export default function LinkGraph({ caseId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)
  const [selected, setSelected] = useState(null)
  // Positions the analyst has moved. The computed layout stays untouched
  // underneath, so "Reset layout" is just dropping this map.
  const [dragged, setDragged] = useState({})
  const [dragging, setDragging] = useState(false)
  const svgRef = useRef(null)
  const drag = useRef(null)

  // Client pixels are not SVG units once the viewBox scales, so every pointer
  // position goes through the SVG's own transform or the node lands elsewhere.
  const toSvg = useCallback((ev) => {
    const svg = svgRef.current
    if (!svg) return null
    const pt = svg.createSVGPoint()
    pt.x = ev.clientX; pt.y = ev.clientY
    const ctm = svg.getScreenCTM()
    if (!ctm) return null
    const p = pt.matrixTransform(ctm.inverse())
    return [p.x, p.y]
  }, [])

  const startDrag = useCallback((ev, id) => {
    const at = toSvg(ev)
    if (!at) return
    ev.preventDefault()
    // Capture is an optimisation, not a requirement: the drag is tracked on
    // window either way. It throws for an unrecognised pointer id, and a throw
    // here would abort the drag before it started.
    try { ev.currentTarget.setPointerCapture?.(ev.pointerId) } catch { /* not fatal */ }
    drag.current = { id, pointerId: ev.pointerId, moved: false }
    setDragging(true)
  }, [toSvg])

  useEffect(() => {
    if (!dragging) return undefined
    const move = (ev) => {
      const d = drag.current
      if (!d) return
      const at = toSvg(ev)
      if (!at) return
      d.moved = true
      setDragged((prev) => ({ ...prev, [d.id]: at }))
    }
    const up = () => { drag.current = null; setDragging(false) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
  }, [dragging, toSvg])

  useEffect(() => {
    setData(null); setError(null); setSelected(null); setDragged({})
    api.caseLinks(caseId).then(setData).catch((e) => setError(e.message))
  }, [caseId])

  const layout = useMemo(() => {
    if (!data) return null
    const pos = { customer: [CX, CY] }
    const inner = data.nodes.filter((n) => n.kind === 'destination' || n.kind === 'device')
    inner.forEach((n, i) => { pos[n.id] = polar(120, (2 * Math.PI * i) / Math.max(inner.length, 1) - Math.PI / 2) })
    const outer = data.nodes.filter((n) => n.kind === 'other_customer')
    // Place each other customer near the first identifier it shares.
    const angleOf = (id) => {
      const p = pos[id]
      return p ? Math.atan2(p[1] - CY, p[0] - CX) : 0
    }
    const crowd = {}
    outer.forEach((n) => {
      const edge = data.edges.find((e) => e.from === n.id)
      const base = edge ? angleOf(edge.to) : 0
      const k = crowd[edge?.to] = (crowd[edge?.to] || 0) + 1
      pos[n.id] = polar(190, base + (k - 1) * 0.22 - 0.11)
    })
    return pos
  }, [data])

  if (error) return <div className="card"><h3>Connections</h3><p className="dim">{error}</p></div>
  if (!data || !layout) return <div className="card"><h3>Connections</h3><p className="dim">Loading connections…</p></div>

  const s = data.summary
  const destinations = data.nodes.filter((n) => n.kind === 'destination')
  const devices = data.nodes.filter((n) => n.kind === 'device')
  const others = data.nodes.filter((n) => n.kind === 'other_customer')
  const at = (id) => dragged[id] || layout[id]
  const selectedNode = data.nodes.find((n) => n.id === selected) || null
  const focus = selected || hover

  return (
    <div className="card">
      <div className="between wrap">
        <h3 style={{ margin: 0 }}>Connections · last {data.days} days</h3>
        <span className="dim" style={{ fontSize: 12 }}>
          {s.shared_destinations} of {s.destinations} destinations and {s.shared_devices} of {s.devices} devices shared with
          {' '}{s.linked_customers} other customer{s.linked_customers === 1 ? '' : 's'}
          {s.linked_confirmed_fraud ? ` · ${s.linked_confirmed_fraud} with confirmed fraud` : ''}
        </span>
      </div>

      {/* What the picture is for. Without this it is an attractive diagram that
          nobody can act on: the question it answers is whether this customer is
          alone, or one of several paying the same new account from the same
          device — which is the difference between one victim and a network. */}
      <p className="lg-explain">
        This customer sits in the middle. Around them are the <b>destinations</b> they paid and
        the <b>devices</b> they used; further out are <b>other customers</b> who share one of
        those. A destination several customers paid within a day, or a device used by more than
        one customer, is what a mule network looks like from the inside.
        {' '}<span className="dim">Drag any node to untangle it.</span>
      </p>

      <div className="linkgraph">
        <div className="lg-canvas">
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
               className={dragging ? 'dragging' : ''}
               aria-label="Graph of this customer, the destinations they paid, the devices they used, and other customers sharing them">
            {data.edges.map((e, i) => {
              const a = at(e.from); const b = at(e.to)
              if (!a || !b) return null
              const hot = focus && (e.from === focus || e.to === focus)
              // An edge between two non-central nodes is a *shared* identifier,
              // which is the finding; an edge from the customer is just their
              // own activity. Colouring both the same hid the interesting one.
              const shared = e.from !== 'customer' && e.to !== 'customer'
              return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]}
                           className={`lg-edge ${e.kind} ${shared ? 'shared' : ''} ${hot ? 'hot' : ''}`} />
            })}
            {data.nodes.map((n) => {
              const [x, y] = at(n.id) || [CX, CY]
              const r = n.kind === 'customer' ? 18 : n.kind === 'other_customer' ? 9 : 12
              const label = n.kind === 'customer' ? n.label
                : n.kind === 'other_customer' ? n.label : short(n.token)
              const cls = `lg-node ${n.kind} ${n.risky ? 'risky' : ''} ${selected === n.id ? 'picked' : ''}`
              return (
                <g key={n.id} className="lg-g"
                   onPointerDown={(ev) => startDrag(ev, n.id)}
                   onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}
                   onClick={() => setSelected((p) => (p === n.id ? null : n.id))}>
                  {n.kind === 'device'
                    ? <rect x={x - r} y={y - r} width={2 * r} height={2 * r} rx={3} className={cls} />
                    : <circle cx={x} cy={y} r={r} className={cls} />}
                  <text x={x} y={y + r + 12} textAnchor="middle" className="lg-label">{label}</text>
                </g>
              )
            })}
          </svg>
          {Object.keys(dragged).length > 0 && (
            <button className="lg-reset ghost" onClick={() => setDragged({})}>Reset layout</button>
          )}
        </div>

        <div className="lg-legend">
          <span><i className="lg-key customer" /> this customer</span>
          <span><i className="lg-key destination" /> destination paid</span>
          <span><i className="lg-key device square" /> device used</span>
          <span><i className="lg-key other_customer" /> another customer</span>
          <span><i className="lg-key risky" /> something known against it</span>
          <span><i className="lg-key line shared" /> shared identifier</span>
        </div>
      </div>

      {/* Clicking a node answers "what is this, and why should I care", which
          the token alone never did. */}
      {selectedNode && <NodeDetail node={selectedNode} onClose={() => setSelected(null)} />}

      {destinations.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Destinations this customer paid</h3>
          <div className="reputation">
            {destinations.map((d) => (
              <div key={d.id} className={`rep ${d.risky ? 'risky' : ''} ${hover === d.id ? 'hot' : ''}`}
                   onMouseEnter={() => setHover(d.id)} onMouseLeave={() => setHover(null)}>
                <span className="mono">{short(d.token)}</span>
                <span className="badges">
                  <span className="badge-chip">first seen {ago(d.first_seen)}</span>
                  <span className="badge-chip">{d.payments} payment{d.payments === 1 ? '' : 's'} · {nairaShort(d.amount_minor)}</span>
                  {d.other_customers > 0 && <span className={`badge-chip ${d.other_customers_24h >= 2 ? 'warn' : ''}`}>
                    paid by {d.other_customers} other customer{d.other_customers === 1 ? '' : 's'}{d.other_customers_24h ? `, ${d.other_customers_24h} today` : ''}</span>}
                  {d.alerts > 0 && <span className="badge-chip">{d.alerts} alert{d.alerts === 1 ? '' : 's'} on payments to it</span>}
                  {d.confirmed_fraud_cases > 0 && <span className="badge-chip danger">{d.confirmed_fraud_cases} confirmed fraud case{d.confirmed_fraud_cases === 1 ? '' : 's'}</span>}
                  {(d.lists || []).map((l) => <span key={l} className="badge-chip danger">{l.replace('_', ' ').toLowerCase()} list</span>)}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {devices.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Devices this customer used</h3>
          <div className="reputation">
            {devices.map((d) => (
              <div key={d.id} className={`rep ${d.risky ? 'risky' : ''}`}
                   onMouseEnter={() => setHover(d.id)} onMouseLeave={() => setHover(null)}>
                <span className="mono">{short(d.token)}</span>
                <span className="badges">
                  <span className="badge-chip">first used by this customer {ago(d.first_used_by_customer)}</span>
                  <span className="badge-chip">{d.uses} use{d.uses === 1 ? '' : 's'}</span>
                  {d.other_customers > 0 && <span className={`badge-chip ${d.other_customers >= 2 ? 'warn' : ''}`}>
                    also used by {d.other_customers} other customer{d.other_customers === 1 ? '' : 's'}</span>}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {others.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Other customers linked to this one</h3>
          <div className="reputation">
            {others.map((o) => (
              <div key={o.id} className={`rep ${o.risky ? 'risky' : ''}`}
                   onMouseEnter={() => setHover(o.id)} onMouseLeave={() => setHover(null)}>
                <span>{o.label}</span>
                <span className="badges">
                  <span className="badge-chip">shares a {o.via.join(' and a ')}</span>
                  {o.case_id
                    ? <Link className={`badge-chip ${o.case_outcome === 'CONFIRMED_FRAUD' ? 'danger' : ''}`} to={`/triage?case=${o.case_id}`}>
                        case #{o.case_id} · {o.case_outcome ? o.case_outcome.replace('_', ' ').toLowerCase() : (o.case_state || '').replace('_', ' ').toLowerCase()} →
                      </Link>
                    : <span className="badge-chip">no case</span>}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {!destinations.length && !devices.length && (
        <p className="dim" style={{ margin: '10px 0 0' }}>No destinations or devices recorded for this customer in the window.</p>
      )}
      <p className="dim" style={{ fontSize: 11.5, margin: '12px 0 0' }}>
        Identifiers are hashed; the same hash means the same account or device. A shared destination is normal for a
        school or a utility; it matters when the destination is new and several customers paid it the same day.
      </p>
    </div>
  )
}

/**
 * What a node actually is, on click. The graph could only ever say "…a1b2c3",
 * which tells an analyst nothing they can act on; every field here already came
 * back from /v1/cases/{id}/links and was simply never shown.
 */
function NodeDetail({ node, onClose }) {
  const rows = []
  if (node.kind === 'destination') {
    rows.push(['Payments from this customer', `${node.payments} · ${nairaShort(node.amount_minor)}`])
    rows.push(['First paid', when(node.first_seen)])
    rows.push(['Last paid', when(node.last_paid)])
    rows.push(['Other customers who paid it', node.other_customers ?? 0])
    if (node.other_customers_24h) {
      rows.push(['…within 24 hours', `${node.other_customers_24h} — the fan-in pattern`])
    }
    rows.push(['Alerts naming it', node.alerts ?? 0])
    if (node.confirmed_fraud_cases) {
      rows.push(['Confirmed fraud cases', `${node.confirmed_fraud_cases} — already proven`])
    }
    if (node.lists?.length) rows.push(['On a list', node.lists.join(', ')])
  } else if (node.kind === 'device') {
    rows.push(['Times used', node.uses])
    rows.push(['First used by this customer', when(node.first_used_by_customer)])
    rows.push(['Last used', when(node.last_used)])
    rows.push(['Other customers on this device', node.other_customers ?? 0])
  } else if (node.kind === 'other_customer') {
    rows.push(['Shares', (node.via || []).join(' and ') || 'an identifier'])
    if (node.case_id) rows.push(['Their case', `#${node.case_id} · ${String(node.case_state || '').toLowerCase()}`])
    if (node.case_outcome) rows.push(['Outcome', String(node.case_outcome).replace(/_/g, ' ').toLowerCase()])
  } else if (node.detail) {
    rows.push(['Activity', node.detail])
  }

  const title = node.kind === 'customer' ? node.label
    : node.kind === 'other_customer' ? node.label
      : node.kind === 'device' ? 'Device' : 'Destination account'

  return (
    <div className="lg-detail">
      <div className="between">
        <div>
          <strong style={{ fontSize: 13 }}>{title}</strong>
          {node.token && <span className="mono dim" style={{ fontSize: 11, marginLeft: 8 }}>{node.token}</span>}
        </div>
        <button className="ghost" onClick={onClose} aria-label="Close">Close</button>
      </div>
      {node.risky && (
        <p className="lg-detail-warn">
          {node.kind === 'destination'
            ? 'Flagged: it is on a list, has confirmed fraud against it, or several customers paid it within a day.'
            : node.kind === 'device'
              ? 'Flagged: more than one customer has used this device.'
              : 'Flagged: this customer has a confirmed fraud case.'}
        </p>
      )}
      <dl className="lg-kv">
        {rows.map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>
        ))}
      </dl>
    </div>
  )
}
