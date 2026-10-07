/** Small shared pieces. */

import { DECISION, HOLD_REASON, MEASURE, RULE, RULE_EFFECT, say } from '../lib/words'

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
    <span className={`pill ${cls}`} title={say(RULE_EFFECT, signal.power)}>
      {say(RULE, signal.code)}
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
  return <div className={`banner ${kind}`} role={kind === 'error' || kind === 'warn' ? 'alert' : 'status'}>{children}</div>
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
    return <p className="dim">No single measurement made a noticeable difference to this score.</p>
  }
  const max = Math.max(...entries.map(([, v]) => Math.abs(v)))

  return (
    <div>
      {entries.map(([name, value]) => (
        <div className="attribution" key={name}>
          <span>{say(MEASURE, name)}</span>
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
          <span className="num" style={{ minWidth: 62 }}>
            {value >= 0 ? 'raised' : 'lowered'}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * How the risk level was reached, step by step, in sentences. The stored trace
 * keeps the model probability, thresholds and rule evidence for the audit; the
 * desk is told what each step did to the risk level.
 */
export function PolicyTrace({ trace }) {
  if (!trace?.length) return null
  return (
    <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
      {trace.map((step, i) => (
        <li key={i} style={{ marginBottom: 6 }}>
          {step.step === 'base' && (
            step.source === 'rule_only_mode' ? (
              <>The risk model was not available, so the written rules decided on their own,
                starting from <RiskBadge level={step.level} />.</>
            ) : step.source === 'receiving_side' ? (
              <>Money coming in is judged only by the rules for incoming payments,
                starting from <RiskBadge level={step.level} />.</>
            ) : (
              <>The risk model rated this payment <RiskBadge level={step.level} />.</>
            )
          )}
          {step.step === 'escalate' && (step.from === step.to
            ? <><strong>{say(RULE, step.code)}</strong> also pointed to fraud; it was already at the
                highest level, <RiskBadge level={step.to} />.</>
            : <><strong>{say(RULE, step.code)}</strong> raised it from <RiskBadge level={step.from} /> to{' '}
                <RiskBadge level={step.to} />.</>
          )}
          {step.step === 'suppress' && (step.from === step.to
            ? <><strong>{say(RULE, step.code)}</strong> suggested it may be normal; it was already at the
                lowest level, <RiskBadge level={step.to} />.</>
            : <><strong>{say(RULE, step.code)}</strong> lowered it from <RiskBadge level={step.from} /> to{' '}
                <RiskBadge level={step.to} />.</>
          )}
          {step.step === 'override' && (
            <><strong>{say(RULE, step.code)}</strong> set it straight to <RiskBadge level={step.to} />.
              This rule always wins, whatever the model or the other rules say.</>
          )}
          {step.step === 'budget' && (
            step.verdict === 'DEFER'
              ? <>It was held back for later, because {say(HOLD_REASON, step.reason)}.</>
              : ({
                  WITHIN_BUDGET: "It went to the team straight away, within today's review limit.",
                  MANDATORY: 'It went to the team straight away: this kind of alert is always raised, whatever the daily limit.',
                  MACHINE: 'Risk Radar handled it automatically, so it did not count against the daily review limit.',
                  NOT_ENFORCED: 'It went to the team straight away (the daily review limit is switched off).',
                }[step.reason] || 'It went to the team straight away.')
          )}
          {step.step === 'final' && (
            <>Final risk <RiskBadge level={step.level} />: {say(DECISION, step.decision).toLowerCase()}
              {step.actionable ? ', and an alert was raised for the team.' : '. Too low to raise an alert.'}</>
          )}
        </li>
      ))}
    </ol>
  )
}
