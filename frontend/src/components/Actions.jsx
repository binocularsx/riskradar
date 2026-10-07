/**
 * Ownership, next steps, and what each button actually does.
 *
 * Three questions an analyst had no way to answer on the first version of this
 * screen:
 *
 *   "Is this mine?"          -> OwnershipBanner
 *   "What do I do now?"      -> the step checklist (CaseFlow)
 *   "What happens if I press that?" -> the consequence line under each button
 *
 * None of it is clever. All of it was missing.
 */


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
        <strong>This case is assigned to you.</strong> Review the evidence and propose an outcome for a different lead to approve.
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
      <strong>{summary.assignee_name || 'Another investigator'} is working this case.</strong> You can read
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
    text = 'You can propose an outcome, but a different lead must approve it. Closure is a separate step once its requirements are met.'
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
    'Proposes confirmed fraud for a different lead to review. Nothing reaches the support team until a lead approves it.',
  FALSE_POSITIVE:
    'Proposes that no fraud was found. The outcome is recorded only after a different lead approves it.',
  INCONCLUSIVE:
    'Proposes an inconclusive finding because the evidence is insufficient. A different lead reviews your reasoning.',
}
