import { ATTRIBUTION, SHAPES, VIEW } from '../lib/nigeriaStates'
import { stateName } from '../lib/geo'

/**
 * Nigeria drawn as its 37 state outlines, for every location view.
 *
 * Shapes come from geoBoundaries (CC BY 4.0, see lib/nigeriaStates.js), and the
 * attribution is printed under the map. Data states are filled by the caller;
 * every other state stays a quiet grey so the country is always recognisable.
 *
 * `children` is a function given the geometry — `at(code)` returns a state's
 * centre, `width`/`height` the drawing box — so a view can lay arrows, symbols
 * or off-map nodes over the same coordinates. `gutter` widens the drawing to the
 * right for things that are not in Nigeria at all, such as accounts at other banks.
 */

const BASE = '#e6e8eb'
const EDGE = '#ffffff'

// Labels that would sit on a neighbour or on the coast are nudged.
const LABEL_SHIFT = {
  'NG-LA': [0, 24], 'NG-OG': [-6, -6], 'NG-AB': [18, 6], 'NG-IM': [-14, 2], 'NG-AN': [-12, -8],
  'NG-RI': [-6, 18], 'NG-FC': [0, 2], 'NG-EB': [6, 4],
}

export const at = (code) => {
  const s = SHAPES[code]
  return s ? [s.cx, s.cy] : null
}

export default function StateMap({
  fill = () => null, stroke = () => null, labels = [], labelText = (c) => stateName(c),
  picked = null, onPick, gutter = 0, title, children, footer,
}) {
  const width = VIEW.width + gutter
  const height = VIEW.height
  const pick = (code) => onPick && onPick(code)
  return (
    <figure className="state-map">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title || 'Map of Nigeria by state'}>
        {gutter > 0 && <rect x={VIEW.width + 12} y={0} width={gutter - 12} height={height} rx={14} className="state-map-gutter" />}
        <g>
          {Object.entries(SHAPES).map(([code, s]) => {
            const f = fill(code)
            const active = !!f
            const on = picked === code
            return (
              <path key={code} d={s.d}
                    fill={f || BASE} stroke={on ? '#111827' : (stroke(code) || EDGE)}
                    strokeWidth={on ? 2.6 : stroke(code) ? 2 : 0.9} strokeLinejoin="round"
                    className={`state-shape ${active && onPick ? 'clickable' : ''}`}
                    tabIndex={active && onPick ? 0 : undefined}
                    role={active && onPick ? 'button' : undefined}
                    aria-label={active ? stateName(code) : undefined}
                    onClick={active ? () => pick(code) : undefined}
                    onKeyDown={active && onPick ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), pick(code)) : undefined}>
                <title>{stateName(code)}</title>
              </path>
            )
          })}
          {/* The picked state is drawn again on top so its outline is never hidden by a neighbour's. */}
          {picked && SHAPES[picked] && <path d={SHAPES[picked].d} fill="none" stroke="#111827" strokeWidth={2.6} pointerEvents="none" />}
        </g>
        {children && children({ at, width, height, mapWidth: VIEW.width })}
        <g pointerEvents="none">
          {labels.filter((c) => SHAPES[c]).map((code) => {
            const [x, y] = at(code)
            const [dx, dy] = LABEL_SHIFT[code] || [0, 0]
            return (
              <text key={code} x={x + dx} y={y + dy} textAnchor="middle" className="state-map-label">{labelText(code)}</text>
            )
          })}
        </g>
      </svg>
      <figcaption>
        {footer}
        <span className="state-map-credit">{ATTRIBUTION}. Location is where the payment's session appeared to come from, not where anyone lives.</span>
      </figcaption>
    </figure>
  )
}

/** A curved arrow between two points, ending a little short so the head sits clear of a marker. */
export function arc([x1, y1], [x2, y2], { bend = 0.22, stop = 10 } = {}) {
  const [mx, my] = [(x1 + x2) / 2, (y1 + y2) / 2]
  const [dx, dy] = [x2 - x1, y2 - y1]
  const len = Math.hypot(dx, dy) || 1
  const b = Math.min(90, len * bend)
  const cx = mx - (dy / len) * b
  const cy = my + (dx / len) * b
  const back = Math.hypot(x2 - cx, y2 - cy) || 1
  const ex = x2 - ((x2 - cx) / back) * stop
  const ey = y2 - ((y2 - cy) / back) * stop
  return { d: `M${x1},${y1} Q${cx},${cy} ${ex},${ey}`, mid: [(x1 + 2 * cx + ex) / 4, (y1 + 2 * cy + ey) / 4] }
}

/** Blend two hex colours. */
export function mix(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16))
  const [x, y] = [p(a), p(b)]
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * Math.max(0, Math.min(1, t)))).join(',')})`
}
