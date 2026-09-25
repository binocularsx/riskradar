/**
 * Ownership, next steps, and what each button actually does.
 *
 * Three questions an analyst had no way to answer on the first version of this
 * screen:
 *
 *   "Is this mine?"          -> OwnershipBanner
 *   "What do I do now?"      -> NextSteps
 *   "What happens if I press that?" -> the consequence line under each button
 *
 * None of it is clever. All of it was missing.
 */

const STEPS = {
  'Escalate to the lead and hold the beneficiary': [
    'Escalate to the Fraud Ops lead now — a sanctions match is their call, not yours.',
    'Do not contact the customer. A sanctions hit has reporting rules attached.',
    'Record the outcome as confirmed fraud so the destination is blocked bank-wide.',
  ],
  'Confirm fraud and recall the funds': [
    'Raise a recall on the transfers listed below, newest first — the newest money is the most recoverable.',
    'Call the customer to confirm they did not authorise it.',
    'Record confirmed fraud. The destination is already on the mule list; this adds the rest.',
  ],
  'Block the card and reissue': [
    'Block the card immediately. The card is compromised, not the account.',
    'Check whether any large authorisation went through after the small probes.',
    'Call the customer to arrange a reissue, then record the outcome.',
  ],
  'Call the customer before any further transfer clears': [
    'Call the number on file — not any number in the transaction.',
    'Ask whether they made these transfers and whether they still hold their phone.',
    'If unreachable, escalate to the lead: an unreachable customer during an active drain is the worst case.',
    'Record the outcome either way, so the next model learns from it.',
  ],
  'Review the destination accounts for a mule network': [
    'Open the timeline below and check whether the destinations are all new.',
    'Search each destination in Search to see whether other customers paid it too.',
    'If several customers fed the same new account, escalate to Fraud Ops as a network.',
    'Otherwise call the customer to confirm the payments were theirs.',
  ],
  'Verify quickly, then clear': [
    'Check the timeline — is the pattern consistent with what this customer normally does?',
    'No customer call needed unless something else looks wrong.',
    'Record false positive. That answer is training data, so recording it is the useful part.',
  ],
  'Contact the customer to verify': [
    'Call the number on file and confirm the transactions were theirs.',
    'If confirmed legitimate, record false positive.',
    'If they did not make them, record confirmed fraud and raise a recall.',
  ],
  'Review the timeline, then contact the customer if it continues': [
    'Read the timeline below before doing anything else.',
    'If the activity has stopped and nothing else stands out, monitor rather than call.',
    'If it is still running, call the customer.',
    'Record an outcome either way — leaving it open helps nobody.',
  ],
  'Monitor — no action needed unless it repeats': [
    'No customer contact needed.',
    'Record the outcome so the case leaves the queue.',
    'If the same customer reappears within the day, treat the pair together.',
  ],
}

const FALLBACK = [
  'Read the timeline below and decide whether the pattern looks like the customer.',
  'Call the customer if anything is unexplained.',
  'Record an outcome so the case leaves the queue.',
]

export function NextSteps({ recommendation }) {
  const steps = STEPS[recommendation?.action] || FALLBACK
  return (
    <div className="card" style={{ background: 'var(--bg-2)' }}>
      <h3 style={{ marginBottom: 10 }}>What to do next</h3>
      <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13.5 }}>
        {steps.map((s, i) => (
          <li key={i} style={{ marginBottom: 7 }}>{s}</li>
        ))}
      </ol>
    </div>
  )
}

/**
 * Who owns this case, and what the reader can do about it.
 *
 * "Am I handling just the alerts, or cases assigned to me?" is a question the
 * screen should never have left open. The answer is: cases, and only the ones
 * with your name on them — unless you take one from the pool.
 */
export function OwnershipBanner({ summary, user, onTake, busy }) {
  const mine = summary.assignee_id === user.id
  const unassigned = !summary.assignee_id

  if (mine) {
    return (
      <div className="banner ok" style={{ marginBottom: 14 }}>
        <strong>This case is assigned to you.</strong> Work it, record an outcome,
        and it leaves your queue.
      </div>
    )
  }
  if (unassigned) {
    return (
      <div className="banner info" style={{ marginBottom: 14 }}>
        <div className="between wrap" style={{ gap: 12 }}>
          <span>
            <strong>Nobody is working this yet.</strong> It is in the shared pool —
            take it and it becomes yours, so no one else duplicates the work.
          </span>
          <button className="primary" onClick={onTake} disabled={busy}>
            Take this case
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="banner warn" style={{ marginBottom: 14 }}>
      <strong>{summary.assignee_name} is working this case.</strong> You can read
      it, but do not act on it — two people calling the same customer is worse
      than nobody calling them.
    </div>
  )
}

/** One line telling the reader exactly what their role lets them finish. */
export function RoleCapability({ user }) {
  const can = (p) => user.permissions.includes(p)

  let text
  if (can('cases:close')) {
    text = 'As Fraud Ops Lead you can record the outcome and close the case in one action.'
  } else if (can('cases:submit_outcome')) {
    // Was `cases:set_outcome`, which is not a permission the server issues, so
    // this branch never matched and an analyst fell through to the line below —
    // being told they could not declare fraud, directly above the buttons that do.
    text = 'As an Analyst you propose the outcome; a Fraud Ops Lead who did not write it decides.'
  } else if (can('cases:escalate')) {
    text = 'Your role can investigate and escalate, but not declare something commercial fraud — '
         + 'that outcome is a training label and belongs to the fraud team.'
  } else {
    text = 'Your role can view this case but not act on it.'
  }
  return <p className="dim" style={{ fontSize: 12, margin: '0 0 4px' }}>{text}</p>
}

/** What pressing each button actually causes. Stated, not implied. */
export const CONSEQUENCES = {
  CONFIRMED_FRAUD:
    'Records this as real fraud. Every destination on the case is added to the '
    + 'known-mule list, so the next transfer to any of them is vetoed outright.',
  FALSE_POSITIVE:
    'Records that the alert was wrong. This is the label the next model is '
    + 'trained on, so it is as valuable as catching fraud.',
  INCONCLUSIVE:
    'Records that the evidence did not settle it. Use this honestly — a guess '
    + 'recorded as certainty poisons the training data.',
}
