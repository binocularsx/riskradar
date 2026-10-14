import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { nairaShort, when } from '../lib/api'
import { stateName } from '../lib/geo'
import { ATTRIBUTION, SHAPES, VIEW } from '../lib/nigeriaStates'
import { OUTCOME, RULE, STAGE, say } from '../lib/words'

/**
 * Case network (D115): one canvas for who, what and where.
 *
 * The same nodes — the customer, the accounts they paid, the devices they used,
 * the other customers tied to those, and similar cases nearby — are laid out
 * two ways. **Network** places them by their links, so a shared account or a
 * device used by several people sits visibly in the middle of its cluster.
 * **Map** places people and cases on the state their sessions came from, and
 * puts accounts at other banks and devices, which have no location, in a column
 * beside Nigeria. Switching animates between the two, so the eye follows a node.
 *
 * The list on the right orders everything by how much it deserves a look, with
 * the reasons in words; the timeline plays the links forward in time.
 *
 * Colour carries identity for three kinds only — this customer (brand orange),
 * other customers (violet), accounts at other banks (green) — validated as a
 * set on the dark surface. Devices and similar cases are neutral and told apart
 * by shape; risk is a labelled ring, never a hue alone.
 */

const W = VIEW.width + 300           // map plus a column for things with no location
const H = VIEW.height
const MAP_W = VIEW.width
const COL_X = MAP_W + 70
// A margin round the drawing so nothing at the edge of Nigeria is cut.
const PAD = { x: 36, y: 30 }
const VB = `${-PAD.x} ${-PAD.y} ${W + PAD.x * 2} ${H + PAD.y * 2}`

export const KIND = {
  customer: { label: 'This customer', colour: '#e0611a' },
  person: { label: 'Other customers', colour: '#9085e9' },
  account: { label: 'Accounts at other banks', colour: '#199e70' },
  device: { label: 'Devices', colour: '#9aa3b2' },
  case: { label: 'Similar cases nearby', colour: '#c4c9d2' },
}
const KIND_ORDER = ['person', 'account', 'device', 'case']

/**
 * Where to look first, in three words rather than a number. The points behind
 * it are hand-set weights for recorded facts — useful for ordering, too rough
 * for a 52 to mean more than a 48 — so the list shows the band and the order,
 * and the detail card shows every point and where it came from.
 */
export const BANDS = [
  { key: 'first', label: 'Look first', min: 50, note: 'Strong recorded reasons to look at this' },
  { key: 'worth', label: 'Worth a look', min: 25, note: 'Some recorded reasons' },
  { key: 'background', label: 'Background', min: 0, note: 'Connected, but little recorded against it' },
]
export const bandOf = (score) => BANDS.find((b) => score >= b.min) || BANDS[BANDS.length - 1]

/** The Devices chip only governs a customer's usual devices; new or shared ones always show. */
export function isVisible(n, layers) {
  if (n.kind === 'customer') return true
  if (n.kind === 'device' && !n.routine) return true
  return !!layers[n.kind]
}

const at = (code) => (SHAPES[code] ? [SHAPES[code].cx, SHAPES[code].cy] : null)
const ms = (t) => (t ? new Date(t).getTime() : null)
const day = (t) => new Date(t).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })

function reduceMotion() {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false }
}

/* ------------------------------------------------------------------ layouts */

/** Deterministic force layout: same graph, same picture, every time. */
function networkLayout(nodes, edges) {
  const cx = W / 2
  const cy = H / 2
  const pos = {}
  const ring = { customer: 0, account: 170, device: 150, person: 300, case: 330 }
  const byKind = {}
  nodes.forEach((n) => { (byKind[n.kind] ||= []).push(n) })
  Object.entries(byKind).forEach(([kind, list]) => {
    list.forEach((n, i) => {
      const a = (i / list.length) * Math.PI * 2 + (kind === 'person' ? 0.3 : kind === 'case' ? 1.1 : 0)
      pos[n.id] = { x: cx + Math.cos(a) * ring[kind], y: cy + Math.sin(a) * ring[kind] * 0.8 }
    })
  })
  const links = edges.filter((e) => pos[e.from] && pos[e.to])
  for (let it = 0; it < 260; it++) {
    const heat = 1 - it / 260
    const force = {}
    nodes.forEach((n) => { force[n.id] = { x: 0, y: 0 } })
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos[nodes[i].id]
        const b = pos[nodes[j].id]
        let dx = a.x - b.x
        let dy = a.y - b.y
        const d2 = Math.max(dx * dx + dy * dy, 40)
        const f = 5200 / d2
        const d = Math.sqrt(d2)
        dx /= d; dy /= d
        force[nodes[i].id].x += dx * f; force[nodes[i].id].y += dy * f
        force[nodes[j].id].x -= dx * f; force[nodes[j].id].y -= dy * f
      }
    }
    links.forEach((e) => {
      const a = pos[e.from]
      const b = pos[e.to]
      const dx = b.x - a.x
      const dy = b.y - a.y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const want = e.kind === 'similar' ? 230 : 120
      const f = (d - want) * 0.04
      force[e.from].x += (dx / d) * f; force[e.from].y += (dy / d) * f
      force[e.to].x -= (dx / d) * f; force[e.to].y -= (dy / d) * f
    })
    nodes.forEach((n) => {
      if (n.kind === 'customer') { pos[n.id] = { x: cx, y: cy }; return }
      const p = pos[n.id]
      const f = force[n.id]
      f.x += (cx - p.x) * 0.012; f.y += (cy - p.y) * 0.016
      const step = Math.min(18 * heat + 1, Math.hypot(f.x, f.y))
      const len = Math.hypot(f.x, f.y) || 1
      p.x = Math.max(60, Math.min(W - 60, p.x + (f.x / len) * step))
      p.y = Math.max(50, Math.min(H - 50, p.y + (f.y / len) * step))
    })
  }
  // Fill the canvas: scale the finished layout to the box, keeping its shape.
  const xs = Object.values(pos).map((p) => p.x)
  const ys = Object.values(pos).map((p) => p.y)
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
  const k = Math.min((W - 260) / Math.max(x1 - x0, 1), (H - 140) / Math.max(y1 - y0, 1), 3)
  Object.values(pos).forEach((p) => {
    p.x = W / 2 + (p.x - (x0 + x1) / 2) * k
    p.y = H / 2 + (p.y - (y0 + y1) / 2) * k
  })
  return pos
}

/** People and cases on their state; accounts and devices in the column beside Nigeria. */
function mapLayout(nodes) {
  const pos = {}
  const perState = {}
  nodes.forEach((n) => {
    if ((n.kind === 'customer' || n.kind === 'person' || n.kind === 'case') && n.state && at(n.state)) {
      (perState[n.state] ||= []).push(n)
    }
  })
  Object.entries(perState).forEach(([code, list]) => {
    const [x, y] = at(code)
    const me = list.find((n) => n.kind === 'customer')
    const rest = list.filter((n) => n !== me)
    if (me) pos[me.id] = { x, y }
    // Concentric rings round the state's centre, each holding more than the
    // last, so a crowded state spreads out instead of stacking marks.
    const CAP = [8, 14, 20, 26]
    let i = 0
    for (let ring = 0; i < rest.length; ring++) {
      const cap = CAP[Math.min(ring, CAP.length - 1)]
      const inRing = Math.min(cap, rest.length - i)
      const r = (me ? 48 : 30) + ring * 34
      for (let k = 0; k < inRing; k++, i++) {
        const a = (k / inRing) * Math.PI * 2 - Math.PI / 2 + ring * 0.35
        pos[rest[i].id] = { x: x + Math.cos(a) * r, y: y + Math.sin(a) * r }
      }
    }
  })
  const column = (list, top, bottom) => list.forEach((n, i) => {
    pos[n.id] = { x: COL_X + 60, y: top + ((bottom - top) * (i + 0.5)) / Math.max(list.length, 1) }
  })
  const accounts = nodes.filter((n) => n.kind === 'account').sort((a, b) => b.others - a.others)
  const devices = nodes.filter((n) => n.kind === 'device')
  const split = devices.length ? 560 : 780
  column(accounts, 60, split)
  column(devices, split + 50, 790)
  // Anything with no known place waits at the foot of the map rather than vanishing.
  nodes.filter((n) => !pos[n.id]).forEach((n, i) => { pos[n.id] = { x: 70 + i * 34, y: H - 40 } })
  return pos
}

/* ---------------------------------------------------------------- the marks */

/** What a ring means, in words, so the label carries it and the ring is only a glance cue. */
export function statusOf(n) {
  if (n.kind === 'customer') return null
  if (n.kind === 'device') {
    if (n.others > 0) return { tone: 'fraud', text: `Shared with ${n.others} other customer${n.others === 1 ? '' : 's'}` }
    if (n.new_device) return { tone: 'new', text: n.flagged ? `New device · ${n.flagged} flagged` : 'New device' }
    return null
  }
  if (n.case_outcome === 'CONFIRMED_FRAUD') return { tone: 'fraud', text: 'Fraud confirmed' }
  if ((n.lists || []).includes('KNOWN_MULE')) return { tone: 'fraud', text: 'Known mule account' }
  if ((n.lists || []).includes('SANCTIONED')) return { tone: 'fraud', text: 'On the sanctions list' }
  if (n.case_id && n.case_state !== 'CLOSED') return { tone: 'open', text: 'Open case' }
  return null
}

const times = (n, one, many) => `${n} ${n === 1 ? one : many}`

/** One line, in words, for what a link means. */
function describeEdge(e, byId) {
  const a = byId[e.from]
  const b = byId[e.to]
  if (!a || !b) return null
  const target = `${b.label}${b.ending ? ` (ending ${b.ending})` : ''}`
  const first = e.time ? `First on ${when(e.time)}` : null
  if (e.kind === 'paid') {
    return { title: `${a.label} paid ${target}`,
             lines: [`${times(e.count || 1, 'payment', 'payments')}${e.value_minor ? ` · ${nairaShort(e.value_minor)}` : ''}`, first] }
  }
  if (e.kind === 'used') {
    return { title: `${a.label} used ${b.label} (a phone or computer)`,
             lines: [`${times(e.count || 1, 'payment', 'payments')} from it in the period`,
                     e.flagged ? `${times(e.flagged, 'flagged payment', 'flagged payments')} came from it` : null,
                     b.new_device ? `New to this customer: ${b.reasons?.find((r) => r.startsWith('New to'))?.replace('New to this customer: ', '') || ''}` : null,
                     b.first_ever ? `First ever used ${when(b.first_ever)}` : first,
                     b.others ? `Also used by ${times(b.others, 'other customer', 'other customers')}` : 'No other customer used it'] }
  }
  return { title: `${b.label} alerted nearby`,
           lines: [`Same state${b.state ? ` (${stateName(b.state)})` : ''}, within two days of this case`,
                   b.shared?.length ? `In common: ${b.shared.map((c) => say(RULE, c)).join('; ')}` : 'No warning sign in common, only place and time',
                   e.time ? `Last alert ${when(e.time)}` : null] }
}

function NodeMark({ n, r, dim, on, hot }) {
  const c = KIND[n.kind].colour
  const ring = n.case_outcome === 'CONFIRMED_FRAUD' || (n.lists || []).length || (n.kind === 'device' && n.others > 0)
  const fresh = n.kind === 'device' && n.new_device && !ring
  const open = n.kind !== 'customer' && n.case_id && n.case_state !== 'CLOSED'
  let shape
  if (n.kind === 'account') shape = <rect x={-r} y={-r} width={r * 2} height={r * 2} rx={4} fill={c} />
  else if (n.kind === 'device') shape = <rect x={-r * 0.8} y={-r * 0.8} width={r * 1.6} height={r * 1.6} rx={2} fill={c} transform="rotate(45)" />
  else if (n.kind === 'case') shape = <circle r={r} fill="#1a1e25" stroke={c} strokeWidth={3.2} strokeDasharray="6 4" />
  else shape = <circle r={r} fill={c} />
  return (
    <g className={`ns-node ${dim ? 'dim' : ''} ${on ? 'on' : ''} ${hot ? 'hot' : ''}`}>
      {on && <circle r={r + 12} className="ns-halo" />}
      {ring && <circle r={r + 7} fill="none" stroke="#f87171" strokeWidth={3.5} />}
      {fresh && <circle r={r + 8} fill="none" stroke="#7dd3fc" strokeWidth={3} />}
      {!ring && open && <circle r={r + 7} fill="none" stroke="#fbbf24" strokeWidth={3} strokeDasharray="5 4" />}
      {/* a 2px surface ring keeps overlapping marks separate */}
      <g stroke="#181c23" strokeWidth={n.kind === 'case' ? 0 : 3}>{shape}</g>
    </g>
  )
}

function edgePath(a, b, bend) {
  const mx = (a.x + b.x) / 2
  const my = (a.y + b.y) / 2
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = Math.hypot(dx, dy) || 1
  const k = Math.min(40, len * bend)
  return `M${a.x},${a.y} Q${mx - (dy / len) * k},${my + (dx / len) * k} ${b.x},${b.y}`
}

/** Where the curve drawn by edgePath is halfway along. */
function edgeMid(a, b, bend) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = Math.hypot(dx, dy) || 1
  const k = Math.min(40, len * bend)
  const cx = (a.x + b.x) / 2 - (dy / len) * k
  const cy = (a.y + b.y) / 2 + (dx / len) * k
  return { x: (a.x + 2 * cx + b.x) / 4, y: (a.y + 2 * cy + b.y) / 4 }
}

/** A phone, so a "used the same device" line says what it is without a key. */
export function PhoneGlyph({ x, y, size = 1 }) {
  return (
    <g transform={`translate(${x},${y}) scale(${size})`} className="ns-phone" aria-hidden="true">
      <rect x={-8} y={-12} width={16} height={24} rx={4} />
      <line x1={-3} x2={3} y1={8} y2={8} />
    </g>
  )
}

/* ---------------------------------------------------------------- the stage */

export default function NetworkStage({ graph, view, onView, layers, selected, onSelect, cursor, onCursor, query }) {
  const nodesAll = graph.nodes
  const edgesAll = graph.edges
  const [hover, setHover] = useState(null)
  const [tip, setTip] = useState(null)
  const [zoom, setZoom] = useState({ k: 1, x: 0, y: 0 })
  const [playing, setPlaying] = useState(false)
  const svgRef = useRef(null)
  const drag = useRef(null)

  const nodes = useMemo(() => nodesAll.filter((n) => isVisible(n, layers)), [nodesAll, layers])
  const ids = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes])
  const edges = useMemo(() => edgesAll.filter((e) => ids.has(e.from) && ids.has(e.to)), [edgesAll, ids])

  const layouts = useMemo(() => ({ network: networkLayout(nodes, edges), map: mapLayout(nodes) }), [nodes, edges])

  // Animate between layouts so a node can be followed from one view to the other.
  const [pos, setPos] = useState(() => layouts[view])
  const posRef = useRef(pos)
  useEffect(() => {
    const target = layouts[view]
    const from = posRef.current
    if (reduceMotion()) { posRef.current = target; setPos(target); return undefined }
    let raf
    const start = performance.now()
    const tick = (now) => {
      const t = Math.min(1, (now - start) / 650)
      const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2
      const next = {}
      Object.keys(target).forEach((id) => {
        const a = from[id] || target[id]
        const b = target[id]
        next[id] = { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e }
      })
      posRef.current = next
      setPos(next)
      if (t < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [layouts, view])

  // Neighbours of whatever is being looked at.
  const focus = hover || selected
  const near = useMemo(() => {
    if (!focus) return null
    const s = new Set([focus])
    edges.forEach((e) => { if (e.from === focus) s.add(e.to); if (e.to === focus) s.add(e.from) })
    if (focus !== 'me') s.add('me')
    return s
  }, [focus, edges])

  const q = (query || '').trim().toLowerCase()
  const matches = (n) => q && `${n.label} ${n.ending || ''} ${n.state ? stateName(n.state) : ''}`.toLowerCase().includes(q)

  // Time: what existed by the cursor.
  const span = [ms(graph.span.start), ms(graph.span.end)]
  const cur = cursor ?? span[1]
  const shown = (t) => !t || ms(t) <= cur

  useEffect(() => {
    if (!playing || !span[0]) return undefined
    const step = (span[1] - span[0]) / 180
    const timer = setInterval(() => {
      onCursor((c) => {
        const next = (c ?? span[0]) + step
        if (next >= span[1]) { setPlaying(false); return span[1] }
        return next
      })
    }, 60)
    return () => clearInterval(timer)
  }, [playing, span[0], span[1], onCursor])

  const maxCount = Math.max(1, ...edges.map((e) => e.count || 1))
  // The drawing is shown at about half size, so marks are drawn at about twice.
  const radius = (n) => (n.kind === 'customer' ? 26 : n.kind === 'person' ? 11 + (n.attention / 100) * 9
    : n.kind === 'account' ? 12 + Math.min(8, (n.others || 0) * 0.8) : n.kind === 'case' ? 13 : 11)

  // Labels are selective: the customer, what is focused and its neighbours, matches, and the strongest few.
  const strongest = useMemo(() => new Set([...nodes]
    .filter((n) => n.kind !== 'customer' && (view === 'network' || n.kind !== 'case'))
    .sort((a, b) => b.attention - a.attention).slice(0, 4).map((n) => n.id)), [nodes, view])
  // Hubs — anything three or more others connect through — are always named:
  // they are what the network is about.
  const degree = useMemo(() => {
    const d = {}
    edges.forEach((e) => { d[e.from] = (d[e.from] || 0) + 1; d[e.to] = (d[e.to] || 0) + 1 })
    return d
  }, [edges])
  const labelled = (n) => n.kind === 'customer' || ['fraud', 'new'].includes(statusOf(n)?.tone)
    || (near ? near.has(n.id) : strongest.has(n.id) || (degree[n.id] || 0) >= 4) || matches(n)

  /* zoom and pan */
  const toSvg = (evt) => {
    // Screen to drawing coordinates, through the same fit the browser applies.
    const r = svgRef.current.getBoundingClientRect()
    const vw = W + PAD.x * 2
    const vh = H + PAD.y * 2
    const k = Math.min(r.width / vw, r.height / vh)
    const ox = (r.width - vw * k) / 2
    const oy = (r.height - vh * k) / 2
    return { x: (evt.clientX - r.left - ox) / k - PAD.x, y: (evt.clientY - r.top - oy) / k - PAD.y }
  }
  const zoomBy = (f, c = { x: W / 2, y: H / 2 }) => setZoom((z) => {
    const k = Math.max(0.6, Math.min(4, z.k * f))
    return { k, x: c.x - ((c.x - z.x) * k) / z.k, y: c.y - ((c.y - z.y) * k) / z.k }
  })
  const onWheel = (evt) => { evt.preventDefault(); zoomBy(evt.deltaY < 0 ? 1.15 : 1 / 1.15, toSvg(evt)) }
  useEffect(() => {
    const el = svgRef.current
    if (!el) return undefined
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })
  const onDown = (evt) => {
    if (evt.target.closest('.ns-hit')) return
    drag.current = { ...toSvg(evt), z: zoom }
  }
  const onMove = (evt) => {
    if (!drag.current) return
    const p = toSvg(evt)
    setZoom({ ...drag.current.z, x: drag.current.z.x + p.x - drag.current.x, y: drag.current.z.y + p.y - drag.current.y })
  }
  const onUp = () => { drag.current = null }

  const byId = useMemo(() => Object.fromEntries(nodesAll.map((n) => [n.id, n])), [nodesAll])
  const [tipEdge, setTipEdge] = useState(null)
  const place = (evt) => {
    const box = svgRef.current.parentElement.getBoundingClientRect()
    const x = evt.clientX - box.left
    // Keep the tip inside the canvas: flip to the left near the right edge.
    return { x: x > box.width - 280 ? x - 274 : x + 14, y: evt.clientY - box.top + 14 }
  }
  const showTip = useCallback((n, evt) => {
    setHover(n.id)
    setTipEdge(null)
    setTip({ n, ...place(evt) })
  }, [])
  const showEdgeTip = (e, key, evt) => {
    const text = describeEdge(e, byId)
    if (!text) return
    setTipEdge(key)
    setTip({ edge: text, ...place(evt) })
  }

  const me = nodes.find((n) => n.kind === 'customer')
  const ticks = useMemo(() => {
    if (!span[0] || !span[1]) return []
    const out = []
    const days = (span[1] - span[0]) / 86400000
    const every = days > 45 ? 14 : days > 14 ? 7 : days > 5 ? 2 : 1
    for (let t = span[0]; t <= span[1]; t += every * 86400000) out.push(t)
    return out
  }, [span[0], span[1]])
  const pct = (t) => (span[1] > span[0] ? ((t - span[0]) / (span[1] - span[0])) * 100 : 100)
  const hiddenNow = nodes.filter((n) => n.kind !== 'customer' && !shown(n.time)).length

  return (
    <div className="ns-stage-canvas">
      <div className="ns-topbar">
        <div className="ns-switch" role="tablist" aria-label="Layout">
          {[['network', 'Network'], ['map', 'Map']].map(([k, l]) => (
            <button key={k} type="button" role="tab" aria-selected={view === k} className={view === k ? 'on' : ''}
                    onClick={() => onView(k)}>{l}</button>
          ))}
        </div>
        <span className="ns-hint">{view === 'map'
          ? 'People and cases sit on the state their sessions came from. Accounts at other banks and devices have no location, so they sit in the column on the right.'
          : 'Placed by their links: whatever several people share sits between them.'}</span>
        <div className="ns-zoom">
          <button type="button" onClick={() => zoomBy(1.25)} aria-label="Zoom in">+</button>
          <button type="button" onClick={() => zoomBy(1 / 1.25)} aria-label="Zoom out">−</button>
          <button type="button" onClick={() => setZoom({ k: 1, x: 0, y: 0 })} aria-label="Reset the view">⤢</button>
        </div>
      </div>

      <div className="ns-canvas" onMouseLeave={() => { setHover(null); setTip(null) }}>
        <svg ref={svgRef} viewBox={VB} role="img"
             aria-label={`Network of ${nodes.length} people, accounts, devices and cases tied to this case`}
             onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onClick={(e) => { if (!e.target.closest('.ns-hit')) onSelect(null) }}>
          <defs>
            {Object.entries(KIND).map(([k, v]) => (
              <marker key={k} id={`ns-head-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={v.colour} />
              </marker>
            ))}
            <marker id="ns-head-move" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" fill="#e0611a" />
            </marker>
          </defs>
          <g transform={`translate(${zoom.x},${zoom.y}) scale(${zoom.k})`}>
            <g className={`ns-map ${view === 'map' ? 'on' : ''}`}>
              {Object.entries(SHAPES).map(([code, s]) => {
                const alert = me?.alert_states?.includes(code)
                const home = me?.home === code && !alert
                const people = nodes.some((n) => n.kind === 'person' && n.state === code)
                return <path key={code} d={s.d} className={`ns-state ${alert ? 'alert' : ''} ${home ? 'home' : ''} ${people ? 'people' : ''}`}>
                  <title>{stateName(code)}{alert ? ' — the alerts happened here' : home ? ' — where this customer usually pays from' : ''}</title>
                </path>
              })}
              {[...new Set(nodes.map((n) => n.state).filter(Boolean))].map((code) => at(code) && (
                <text key={code} x={at(code)[0]} y={at(code)[1] + (code === me?.state ? 84 : 44)} className="ns-state-label" textAnchor="middle">{stateName(code)}</text>
              ))}
              {me?.home && me.home !== me.state && at(me.home) && (
                <g transform={`translate(${at(me.home)[0]},${at(me.home)[1]})`}>
                  <circle r={9} className="ns-home-dot" />
                  <text x={-14} y={-18} className="ns-state-label">Usually pays from {stateName(me.home)}</text>
                </g>
              )}
              <line x1={MAP_W + 14} x2={MAP_W + 14} y1={20} y2={H - 20} className="ns-divider" />
              <text x={COL_X} y={14} className="ns-col-title">Accounts at</text>
              <text x={COL_X} y={36} className="ns-col-title">other banks</text>
              {nodes.some((n) => n.kind === 'device') && <text x={COL_X} y={598} className="ns-col-title">Devices</text>}
            </g>

            {me?.home && me.home !== me.state && view === 'map' && at(me.home) && pos.me && (
              <path d={edgePath({ x: at(me.home)[0], y: at(me.home)[1] }, pos.me, 0.25)} className="ns-move" markerEnd="url(#ns-head-move)">
                <title>{`Usually pays from ${stateName(me.home)}; the alerts came from ${stateName(me.state)}`}</title>
              </path>
            )}

            <g className="ns-edges">
              {edges.map((e) => {
                const a = pos[e.from]
                const b = pos[e.to]
                if (!a || !b) return null
                // Light the whole path: the focused node's own links, and this
                // customer's link to anything it touches — that is the shared account.
                const lit = near && near.has(e.from) && near.has(e.to)
                  && (e.from === focus || e.to === focus || e.from === 'me')
                const from = nodesAll.find((n) => n.id === e.from)
                const colour = e.kind === 'paid' ? KIND[from?.kind || 'person'].colour
                  : e.kind === 'used' ? '#c4c9d2' : '#9aa3b2'
                const bend = view === 'map' ? 0.18 : 0.08
                const w = e.kind === 'similar' ? 14
                  : e.kind === 'used' ? 2.5
                  : 2 + (Math.log(1 + (e.count || 1)) / Math.log(1 + maxCount)) * 5
                const d = edgePath(a, b, bend)
                const key = `${e.from}-${e.to}-${e.kind}`
                const on = lit || tipEdge === key
                return (
                  <g key={key}>
                    <path d={d}
                          className={`ns-edge ${e.kind} ${near && !lit ? 'dim' : ''} ${on ? 'lit' : ''} ${shown(e.time) ? '' : 'future'}`}
                          stroke={colour} strokeWidth={on ? w + 1.5 : w}
                          markerEnd={e.kind === 'paid' ? `url(#ns-head-${from?.kind || 'person'})` : undefined} />
                    {e.kind === 'used' && (() => {
                      const m = edgeMid(a, b, bend)
                      return <g className={`ns-phone-wrap ${near && !lit ? 'dim' : ''} ${shown(e.time) ? '' : 'future'}`}><PhoneGlyph x={m.x} y={m.y} /></g>
                    })()}
                    {/* the line is thin; the target for pointing at it is not */}
                    <path d={d} className="ns-edge-hit" onMouseEnter={(evt) => showEdgeTip(e, key, evt)}
                          onMouseMove={(evt) => showEdgeTip(e, key, evt)} onMouseLeave={() => { setTip(null); setTipEdge(null) }} />
                  </g>
                )
              })}
            </g>

            <g className="ns-nodes">
              {[...nodes].sort((a, b) => (a.id === focus) - (b.id === focus)).map((n) => {
                const p = pos[n.id]
                if (!p) return null
                const r = radius(n)
                const dim = (near && !near.has(n.id)) || (q && !matches(n) && n.kind !== 'customer')
                return (
                  <g key={n.id} transform={`translate(${p.x},${p.y})`}
                     className={`ns-hit ${n.kind === 'customer' || shown(n.time) ? '' : 'future'}`} tabIndex={0} role="button"
                     aria-label={`${n.label}: ${KIND[n.kind].label.toLowerCase()}. ${n.reasons?.[0] || ''}`}
                     onMouseEnter={(evt) => showTip(n, evt)} onMouseMove={(evt) => showTip(n, evt)}
                     onMouseLeave={() => { setHover(null); setTip(null) }}
                     onFocus={() => setHover(n.id)} onBlur={() => setHover(null)}
                     onClick={(evt) => { evt.stopPropagation(); onSelect(n.id) }}
                     onKeyDown={(evt) => (evt.key === 'Enter' || evt.key === ' ') && (evt.preventDefault(), onSelect(n.id))}>
                    <circle r={r + 12} fill="transparent" />
                    <NodeMark n={n} r={r} dim={dim} on={selected === n.id} hot={matches(n)} />
                    {labelled(n) && (
                      <text x={n.kind === 'customer' && view === 'map' ? 0 : p.x > W * 0.62 ? -(r + 10) : r + 10}
                            y={n.kind === 'customer' && view === 'map' ? -r - 14 : 7}
                            textAnchor={n.kind === 'customer' && view === 'map' ? 'middle' : p.x > W * 0.62 ? 'end' : 'start'}
                            className={`ns-label ${n.kind === 'customer' ? 'me' : ''}`}>
                        {p.x > W * 0.62 && !(n.kind === 'customer' && view === 'map') && statusOf(n) &&
                          <tspan className={`ns-tag ${statusOf(n).tone}`} dx={0}>{statusOf(n).text}  </tspan>}
                        <tspan>{n.label}{n.kind === 'account' ? ` ·${n.ending}` : ''}</tspan>
                        {!(p.x > W * 0.62) && statusOf(n) && <tspan className={`ns-tag ${statusOf(n).tone}`} dx={10}>{statusOf(n).text}</tspan>}
                      </text>
                    )}
                  </g>
                )
              })}
            </g>
          </g>
        </svg>

        {tip?.n && (
          <div className="ns-tip" style={{ left: tip.x, top: tip.y }}>
            <span className="ns-tip-kind">{KIND[tip.n.kind].label.replace(/s$/, '')}</span>
            <strong>{tip.n.label}{tip.n.ending ? ` · ending ${tip.n.ending}` : ''}</strong>
            {statusOf(tip.n) && <span className={`ns-tip-status ${statusOf(tip.n).tone}`}>{statusOf(tip.n).text}</span>}
            {tip.n.state && <span>{stateName(tip.n.state)}</span>}
            {tip.n.kind !== 'customer' && <span className={`ns-tip-band ${bandOf(tip.n.attention).key}`}>{bandOf(tip.n.attention).label}</span>}
            {(tip.n.reasons || []).slice(0, 2).map((r) => <span key={r} className="ns-tip-why">{r}</span>)}
            {tip.n.time && <span className="ns-tip-time">First seen {when(tip.n.time)}</span>}
          </div>
        )}
        {tip?.edge && (
          <div className="ns-tip" style={{ left: tip.x, top: tip.y }}>
            <span className="ns-tip-kind">Link</span>
            <strong>{tip.edge.title}</strong>
            {tip.edge.lines.filter(Boolean).map((l) => <span key={l} className="ns-tip-why">{l}</span>)}
          </div>
        )}

        <Key view={view} />

      </div>
      {span[0] && (
        <div className="ns-timeline">
          <button type="button" className="ns-play" onClick={() => {
            if (!playing && cur >= span[1]) onCursor(span[0])
            setPlaying((p) => !p)
          }} aria-label={playing ? 'Pause' : 'Play the activity through time'}>
            {playing ? '❚❚' : '▶'}
          </button>
          <div className="ns-track">
            <input type="range" min={span[0]} max={span[1]} step={Math.max(1, Math.round((span[1] - span[0]) / 500))}
                   value={cur} onChange={(e) => { setPlaying(false); onCursor(Number(e.target.value)) }}
                   aria-label="Show the activity up to this date" aria-valuetext={day(cur)} />
            <div className="ns-ticks" aria-hidden="true">
              {ticks.map((t) => <span key={t} style={{ left: `${pct(t)}%` }}>{day(t)}</span>)}
            </div>
            <span className="ns-cursor-label" style={{ left: `${pct(cur)}%` }}>{day(cur)}</span>
          </div>
          <span className="ns-time-note">{hiddenNow ? `${hiddenNow} not yet seen by ${day(cur)}` : 'Everything in the period'}</span>
          {cur < span[1] && <button type="button" className="ns-now" onClick={() => { setPlaying(false); onCursor(null) }}>Show all</button>}
        </div>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- the key */

const KEY_SEEN = 'rr_case_network_key_seen'

/**
 * What the rings and lines mean. Open the first time someone uses the tool,
 * then a small button they can open again. What each colour and shape is
 * belongs to the chips above the canvas, which also turn each kind on and off.
 */
function Key({ view }) {
  const [open, setOpen] = useState(() => {
    try { return !window.localStorage.getItem(KEY_SEEN) } catch { return true }
  })
  const close = () => {
    setOpen(false)
    try { window.localStorage.setItem(KEY_SEEN, '1') } catch { /* fine: it opens again next time */ }
  }
  if (!open) {
    return <button type="button" className="ns-key-btn" onClick={() => setOpen(true)} aria-expanded="false">Key</button>
  }
  return (
    <div className="ns-key" role="region" aria-label="Key">
      <div className="ns-key-head">
        <strong>How to read this</strong>
        <button type="button" className="ns-x" onClick={close} aria-label="Close the key">×</button>
      </div>
      <p className="ns-key-note">Colours and shapes are named in the <b>Show</b> chips above, which also hide or show each kind.</p>
      <dl>
        <div><dt><svg width="34" height="10" aria-hidden="true"><line x1="1" y1="5" x2="27" y2="5" stroke="#b3b9c4" strokeWidth="2.5" /><path d="M26,1 L33,5 L26,9 z" fill="#b3b9c4" /></svg></dt><dd>Paid — the arrow points to who received the money</dd></div>
        <div><dt><svg width="34" height="18" viewBox="0 0 34 18" aria-hidden="true"><line x1="1" y1="9" x2="33" y2="9" stroke="#c4c9d2" strokeWidth="2" /><g className="ns-phone" transform="translate(17,9) scale(0.55)"><rect x="-8" y="-12" width="16" height="24" rx="4" /><line x1="-3" x2="3" y1="8" y2="8" /></g></svg></dt><dd>Used this phone or computer. New or shared devices are always shown and tagged; usual ones only when Devices is on</dd></div>
        <div><dt><svg width="34" height="14" aria-hidden="true"><line x1="5" y1="7" x2="29" y2="7" stroke="#9aa3b2" strokeWidth="9" strokeLinecap="round" strokeOpacity="0.28" /></svg></dt><dd>Similar case nearby: same state, within two days — not a payment or a device</dd></div>
        <div><dt><svg width="34" height="16" aria-hidden="true"><circle cx="17" cy="8" r="6.5" fill="none" stroke="#f87171" strokeWidth="2.2" /></svg></dt><dd>Red ring: fraud confirmed, on a watch list, or a device shared with other customers</dd></div>
        <div><dt><svg width="34" height="16" aria-hidden="true"><circle cx="17" cy="8" r="6.5" fill="none" stroke="#fbbf24" strokeWidth="2" strokeDasharray="3 2.5" /></svg></dt><dd>Amber ring: has an open case</dd></div>
        <div><dt><svg width="34" height="16" aria-hidden="true"><circle cx="17" cy="8" r="6.5" fill="none" stroke="#7dd3fc" strokeWidth="2.2" /></svg></dt><dd>Blue ring: a device this customer started using just before the flagged payments</dd></div>
        {view === 'map' && <div><dt><svg width="34" height="10" aria-hidden="true"><line x1="1" y1="5" x2="33" y2="5" stroke="#e0611a" strokeWidth="2.5" strokeDasharray="7 5" /></svg></dt><dd>From where this customer usually pays to where the alerts happened</dd></div>}
      </dl>
      <p className="ns-key-note">Thicker lines mean more payments. Point at any line or mark to see what it is.</p>
    </div>
  )
}

/* -------------------------------------------------------- the ranked list */

export function Ranking({ graph, layers, selected, onSelect, query, onQuery }) {
  const [tab, setTab] = useState('all')
  const q = (query || '').trim().toLowerCase()
  const nodes = graph.nodes.filter((n) => n.kind !== 'customer' && isVisible(n, layers))
  const counts = Object.fromEntries(KIND_ORDER.map((k) => [k, nodes.filter((n) => n.kind === k).length]))
  const tabKinds = KIND_ORDER.filter((k) => counts[k] > 0)
  const rows = nodes
    .filter((n) => tab === 'all' || n.kind === tab)
    .filter((n) => !q || `${n.label} ${n.ending || ''} ${n.state ? stateName(n.state) : ''}`.toLowerCase().includes(q))
    .sort((a, b) => b.attention - a.attention)
  const sel = graph.nodes.find((n) => n.id === selected)

  return (
    <aside className="ns-side" aria-label="Who to look at">
      <div className="ns-side-head">
        <strong>Who to look at</strong>
        <span>Grouped by how much is recorded against each; select one for the reasons</span>
      </div>
      <label className="ns-search">
        <span className="sr-only">Search people, accounts and states</span>
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4" /></svg>
        <input type="search" value={query} onChange={(e) => onQuery(e.target.value)} placeholder="Search a name, account ending or state" />
      </label>
      <div className="ns-tabs" role="tablist">
        {[['all', 'All', nodes.length], ...tabKinds.map((k) => [k, { person: 'People', account: 'Accounts', device: 'Devices', case: 'Cases' }[k], counts[k]])]
          .map(([k, l, n]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>
              {l} <span>{n}</span>
            </button>
          ))}
      </div>

      {sel && (
        <div className="ns-detail">
          <div className="ns-detail-top">
            <span className="ns-kind-chip" style={{ '--c': KIND[sel.kind].colour }}>{KIND[sel.kind].label.replace(/s$/, '')}</span>
            <button type="button" className="ns-x" onClick={() => onSelect(null)} aria-label="Close">×</button>
          </div>
          <strong className="ns-detail-name">{sel.label}{sel.ending ? ` · ending ${sel.ending}` : ''}</strong>
          <dl>
            {sel.state && <div><dt>Where</dt><dd>{stateName(sel.state)}{sel.kind === 'customer' && sel.home && sel.home !== sel.state ? ` — usually ${stateName(sel.home)}` : ''}</dd></div>}
            {sel.detail && <div><dt>What</dt><dd>{sel.detail}</dd></div>}
            {sel.payments != null && <div><dt>Payments</dt><dd>{sel.payments} · {nairaShort(sel.value_minor)}</dd></div>}
            {sel.case_id && sel.kind !== 'customer' && <div><dt>Case</dt><dd>
              {sel.case_visible ? <Link to={`/cases/${sel.case_id}`}>CASE-{sel.case_id}</Link> : <span title="Someone else's case">CASE-{sel.case_id}</span>}
              {' · '}{sel.case_outcome ? say(OUTCOME, sel.case_outcome) : say(STAGE, sel.case_state)}</dd></div>}
            {sel.time && <div><dt>First seen</dt><dd>{when(sel.time)}</dd></div>}
          </dl>
          {sel.kind !== 'customer' && (
            <div className="ns-why-box">
              <div className="ns-why-head">
                <span>Why it is here</span>
                <span className={`ns-band ${bandOf(sel.attention).key}`}>{bandOf(sel.attention).label}</span>
              </div>
              {sel.breakdown?.length ? (
                <ul className="ns-why">
                  {sel.breakdown.map((b) => <li key={b.why}><span>{b.why}</span><span className="ns-pts">+{b.points}</span></li>)}
                  <li className="ns-why-total">
                    <span>Adds up to{sel.breakdown.reduce((s, b) => s + b.points, 0) > 100 ? ' (capped)' : ''}</span>
                    <span className="ns-pts">{sel.attention}</span>
                  </li>
                </ul>
              ) : <p className="ns-why-none">Nothing recorded against it beyond the link itself.</p>}
              <p className="ns-why-note">50 or more is Look first; 25 to 49 is Worth a look; under 25 is Background.</p>
            </div>
          )}
        </div>
      )}

      <div className="ns-list">
        {BANDS.map((band) => {
          const inBand = rows.filter((n) => bandOf(n.attention).key === band.key)
          if (!inBand.length) return null
          return (
            <section key={band.key} className={`ns-band-group ${band.key}`} aria-label={band.label}>
              <h4 className="ns-band-title"><span className={`ns-band ${band.key}`}>{band.label}</span><span>{inBand.length}</span></h4>
              <ol>
                {inBand.map((n) => (
                  <li key={n.id}>
                    <button type="button" className={selected === n.id ? 'on' : ''} onClick={() => onSelect(selected === n.id ? null : n.id)}
                            aria-label={`${n.label}: ${band.label}. ${n.reasons?.[0] || ''}`}>
                      <span className={`ns-swatch ${n.kind}`} style={{ '--c': KIND[n.kind].colour }} aria-hidden="true" />
                      <span className="ns-row-text">
                        <span className="ns-row-name">{n.label}{n.ending ? ` ·${n.ending}` : ''}</span>
                        <span className="ns-row-why">{n.reasons?.[0] || KIND[n.kind].label}</span>
                      </span>
                      <span className="ns-score-bar" aria-hidden="true"><i style={{ width: `${n.attention}%` }} /></span>
                    </button>
                  </li>
                ))}
              </ol>
            </section>
          )
        })}
        {!rows.length && <p className="ns-none">Nothing matches.</p>}
      </div>
      <p className="ns-side-foot">Ordered by recorded reasons, each worth set points — select one to see them. A guide to where to look first, not a risk score from the model. {ATTRIBUTION}.</p>
    </aside>
  )
}
