/** Small shared pieces. */

export function SegmentedProgress({ value, label, color = 'var(--accent)' }) {
  const percent = Math.min(100, Math.max(0, Number(value) || 0))
  return (
    <div className="segmented-progress" role="meter" aria-label={label}
         aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}
         style={{ '--progress-color': color }}>
      {Array.from({ length: 5 }, (_, index) => (
        <span className="progress-step" key={index} aria-hidden="true">
          <span style={{ width: `${Math.min(100, Math.max(0, percent * 5 - index * 100))}%` }} />
        </span>
      ))}
    </div>
  )
}

export function RiskBadge({ level }) {
  if (!level) return <span className="dim">—</span>
  // The word is always present, never colour alone: a queue that can only be
  // triaged by hue is a queue part of the team cannot triage.
  const label = String(level).charAt(0).toUpperCase() + String(level).slice(1).toLowerCase()
  return <span className={`risk risk-${level}`}>{label}</span>
}

export function SignalPill({ signal }) {
  const cls = { ESCALATE: 'escalate', OVERRIDE: 'override', SUPPRESS: 'suppress' }[signal.power] || ''
  return (
    <span className={`pill ${cls}`} title={JSON.stringify(signal.evidence)}>
      {signal.code}
    </span>
  )
}

export function Stat({ label, value, note }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  )
}

export function Banner({ kind = 'info', children }) {
  return <div className={`banner ${kind}`}>{children}</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

/**
 * Local feature attributions (G3).
 *
 * Ablation-based: each bar is how far the predicted probability moves when that
 * feature is replaced by its training-set median. Positive pushed the score up,
 * negative pulled it down. It answers "how much did this feature move this
 * decision", which is the question an analyst actually has.
 */
export function Attributions({ attributions, limit = 8 }) {
  const entries = Object.entries(attributions || {})
    .filter(([, v]) => Math.abs(v) > 1e-6)
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
    .slice(0, limit)

  if (!entries.length) {
    return <p className="dim">No feature moved this decision measurably.</p>
  }
  const max = Math.max(...entries.map(([, v]) => Math.abs(v)))

  return (
    <div>
      {entries.map(([name, value]) => (
        <div className="attribution" key={name}>
          <span className="mono">{name}</span>
          <div className="attribution-bar">
            <span
              className={value >= 0 ? 'pos' : 'neg'}
              style={
                value >= 0
                  ? { left: '50%', width: `${(Math.abs(value) / max) * 50}%` }
                  : { right: '50%', width: `${(Math.abs(value) / max) * 50}%` }
              }
            />
          </div>
          <span className="mono num" style={{ minWidth: 62 }}>
            {value >= 0 ? '+' : ''}
            {value.toFixed(4)}
          </span>
        </div>
      ))}
    </div>
  )
}

/** The policy layer's decision trace, rendered as the sentence it is. */
export function PolicyTrace({ trace }) {
  if (!trace?.length) return null
  return (
    <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
      {trace.map((step, i) => (
        <li key={i} style={{ marginBottom: 6 }}>
          {step.step === 'base' && (
            <>
              {step.source === 'rule_only_mode' ? (
                <>
                  <strong>Rule-only mode</strong> — the model was unavailable, so no
                  probability was used. Starting at <RiskBadge level={step.level} />
                </>
              ) : (
                <>
                  Model probability <span className="mono">{step.p_fraud}</span> against
                  thresholds <span className="mono">{JSON.stringify(step.thresholds)}</span> →{' '}
                  <RiskBadge level={step.level} />
                </>
              )}
            </>
          )}
          {step.step === 'escalate' && (
            <>
              <span className="pill escalate">{step.code}</span> raised {step.from} →{' '}
              <RiskBadge level={step.to} />{' '}
              <span className="evidence">{JSON.stringify(step.evidence)}</span>
            </>
          )}
          {step.step === 'suppress' && (
            <>
              <span className="pill suppress">{step.code}</span> lowered {step.from} →{' '}
              <RiskBadge level={step.to} />{' '}
              <span className="evidence">{JSON.stringify(step.evidence)}</span>
            </>
          )}
          {step.step === 'override' && (
            <>
              <span className="pill override">{step.code}</span> overrode {step.from} →{' '}
              <RiskBadge level={step.to} />. {step.note}
            </>
          )}
          {step.step === 'final' && (
            <>
              Final <RiskBadge level={step.level} /> → decision{' '}
              <strong>{step.decision}</strong>
              {step.actionable ? ' (alert raised)' : ' (below the alert threshold)'}
            </>
          )}
        </li>
      ))}
    </ol>
  )
}
