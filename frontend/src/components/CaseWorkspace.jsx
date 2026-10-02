const ICONS = {
  cases: ['M4 7h16v13H4z', 'M8 7V4h8v3', 'M9 12h6', 'M9 16h4'],
  money: ['M3 6h18v12H3z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6', 'M6 12h.01', 'M18 12h.01'],
  clock: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', 'M12 7v5l3 2'],
  person: ['M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8', 'M5 21v-3a7 7 0 0 1 14 0v3'],
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14', 'M16 16l4 4'],
  arrow: ['M5 12h14', 'm13 6 6 6-6 6'],
  check: ['m5 12 4 4L19 6'],
  transfer: ['M4 7h16', 'm16 3 4 4-4 4', 'M20 17H4', 'm8 13-4 4 4 4'],
  refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M6 6a8 8 0 0 1 13 3', 'M18 18a8 8 0 0 1-13-3'],
  declined: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', 'm9 9 6 6', 'm15 9-6 6'],
  reversed: ['M4 10h10a6 6 0 0 1 0 12', 'm9 5-5 5 5 5'],
}

export function CaseIcon({ name }) {
  return <svg className="case-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {(ICONS[name] || ICONS.cases).map((d, i) => <path key={i} d={d} />)}
  </svg>
}

export function CaseMetric({ label, value, note, icon, featured = false, tone = '' }) {
  return <div className={`statcard ${featured ? 'featured' : ''}`}>
    <div className="statcard-top"><div className="statcard-k">{label}</div>
      <span className="case-metric-icon"><CaseIcon name={icon} /></span></div>
    <div className={`statcard-v ${tone}`}>{value ?? '—'}</div>
    <div className="statcard-note">{note}</div>
  </div>
}
