/**
 * One chart look for every Recharts chart in the console.
 *
 * The charts were first drawn for a dark theme and kept its colours when the
 * console went light: a near-black tooltip with dark label text (unreadable),
 * near-black area fills, and gridlines darker than the cards. Recharts writes
 * these as SVG attributes, where CSS variables are not reliable, so the values
 * are the light theme's tokens copied as literals.
 */

export const CHART = {
  text: '#4b5563',     // --text-2
  muted: '#9ca3af',    // --text-3
  grid: '#e5e7eb',     // --line
  blue: '#2563eb',
  blueFill: '#dbeafe',
  orange: '#DD4F05',   // --accent
  orangeFill: '#fcece3',
  teal: '#0e7490',
}

export const axis = { stroke: CHART.muted, tick: { fill: CHART.text, fontSize: 11 }, tickLine: false }

export const grid = { stroke: CHART.grid, vertical: false }

export const tooltip = {
  contentStyle: {
    background: '#ffffff', border: '1px solid #d3d6db', borderRadius: 8, fontSize: 12,
    boxShadow: '0 6px 20px -10px rgba(17,24,39,.3)', color: '#111827',
  },
  labelStyle: { color: '#111827', fontWeight: 600, marginBottom: 4 },
  itemStyle: { padding: 0 },
  cursor: { fill: 'rgba(17,24,39,.04)' },
}

// Recharts paints legend text in the series colour, which for a pale fill is
// unreadable; the swatch carries the colour, the words stay text-coloured.
export const legend = {
  wrapperStyle: { fontSize: 11.5 },
  formatter: (value) => <span style={{ color: CHART.text }}>{value}</span>,
}
