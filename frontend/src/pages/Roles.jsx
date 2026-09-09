import { api } from '../lib/api'

/**
 * Who does what, and what each of them sees.
 *
 * Four roles exist and until now the only way to find out what the other three
 * could do was to log in as them. This page answers it directly, and it is
 * visible to everybody — including to the roles it constrains, because a
 * control people cannot see is a control they will work around.
 */

const ROLES = [
  {
    id: 'ANALYST',
    title: 'Fraud Analyst',
    who: 'The person who works the queue all day.',
    sees: [
      'Triage — the worklist and the case beside it',
      'Search — look up any transaction',
    ],
    can: [
      'Take a case from the shared pool',
      'Read every piece of evidence on it',
      'Add investigation notes',
      'Record the outcome: confirmed fraud, false positive, or inconclusive',
      'Escalate to InfoSec or to Fraud Ops',
    ],
    cannot: [
      'Close a case — a second pair of eyes closes it',
      'Change any rule or threshold',
      'Create or manage users',
    ],
    why: 'They record what happened; somebody else signs it off. That separation '
       + 'is why an outcome can be trusted as a training label.',
  },
  {
    id: 'FRAUD_OPS_LEAD',
    title: 'Fraud Operations Lead',
    who: 'Runs the desk. Signs off the analysts’ work.',
    sees: [
      'Everything an analyst sees',
      'Operations — backlog, ageing, team load, false-positive rate',
    ],
    can: [
      'Everything an analyst can do',
      'Close cases and reassign them',
      'See whether the desk is coping',
    ],
    cannot: [
      'Change rules or thresholds — tuning detection is a different job',
    ],
    why: 'They are accountable for whether the queue is being cleared, which is '
       + 'why they get the operations view and nobody else on the floor does.',
  },
  {
    id: 'INFOSEC_ANALYST',
    title: 'Information Security Analyst',
    who: 'Handles suspected account compromise rather than commercial loss.',
    sees: [
      'Triage and Search',
      'Operations',
    ],
    can: [
      'Read and investigate any case',
      'Escalate',
      'Add notes',
    ],
    cannot: [
      'Declare something commercial fraud',
      'Close cases',
      'Touch detection settings',
    ],
    why: '"Was this account taken over" and "did this customer lose money" are '
       + 'different questions. Case outcomes are ML training labels, so they '
       + 'belong to the team that owns the fraud definition.',
  },
  {
    id: 'ADMIN',
    title: 'Administrator',
    who: 'Tunes the system. Never touches a case.',
    sees: [
      'Administration — rules, thresholds, models, lists, the audit log',
      'Operations',
      'No queue. No case. Not even read-only.',
    ],
    can: [
      'Enable, disable and retune rules',
      'Publish new threshold versions',
      'Promote or roll back a model',
      'Manage users and API keys',
      'Verify the audit chain',
    ],
    cannot: [
      'Open, read, review, decide or close any case',
    ],
    why: 'This is the sharpest rule in the system. Someone who can loosen a '
       + 'threshold and then inspect the cases that threshold failed to produce '
       + 'could quietly tune their own work out of view. So they get neither.',
  },
]

export default function Roles({ user }) {
  return (
    <>
      <div className="banner info" style={{ marginBottom: 18 }}>
        You are signed in as <strong>{user.display_name}</strong> —{' '}
        {user.role.replace(/_/g, ' ').toLowerCase()}. The card for your role is
        outlined below.
      </div>

      <div className="grid cols-2">
        {ROLES.map((r) => {
          const isMe = r.id === user.role
          return (
            <div className="card" key={r.id}
                 style={isMe ? { borderColor: 'var(--accent)', boxShadow: '0 0 0 1px var(--accent)' } : undefined}>
              <div className="between" style={{ marginBottom: 6 }}>
                <h2 style={{ margin: 0 }}>{r.title}</h2>
                {isMe && <span className="pill">you</span>}
              </div>
              <p className="muted" style={{ fontSize: 13, marginBottom: 14 }}>{r.who}</p>

              <h3>Screens they see</h3>
              <ul style={{ margin: '0 0 14px', paddingLeft: 20, fontSize: 13 }}>
                {r.sees.map((s) => <li key={s} style={{ marginBottom: 4 }}>{s}</li>)}
              </ul>

              <h3>Can</h3>
              <ul style={{ margin: '0 0 14px', paddingLeft: 20, fontSize: 13 }}>
                {r.can.map((s) => <li key={s} style={{ marginBottom: 4 }}>{s}</li>)}
              </ul>

              <h3 style={{ color: 'var(--critical)' }}>Cannot</h3>
              <ul style={{ margin: '0 0 14px', paddingLeft: 20, fontSize: 13 }}>
                {r.cannot.map((s) => <li key={s} style={{ marginBottom: 4 }}>{s}</li>)}
              </ul>

              <p className="dim" style={{ fontSize: 12, marginBottom: 0,
                                          borderTop: '1px solid var(--line)', paddingTop: 10 }}>
                {r.why}
              </p>
            </div>
          )
        })}
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <h2>These limits are enforced by the server, not the menu</h2>
        <p className="muted" style={{ fontSize: 13.5, marginBottom: 10 }}>
          The missing links in the sidebar are a convenience. Every one of the
          restrictions above is checked again on the API, on every request. An
          administrator who types the case URL directly gets a refusal, not a
          case — and the refusal says why.
        </p>
        <p className="dim mono" style={{ fontSize: 12, marginBottom: 0 }}>
          403 — role ADMIN does not hold cases:read. This is separation of duties, not a bug.
        </p>
      </div>
    </>
  )
}
