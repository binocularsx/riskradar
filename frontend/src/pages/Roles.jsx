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
      'Cases to review — the work assigned to them',
      'Find a transaction — look up any transaction',
    ],
    can: [
      'Take a case from the shared pool',
      'Read every piece of evidence on it',
      'Add investigation notes',
      'Suggest a result: fraud confirmed, no fraud found, or more review needed',
      'Send a case to the Security or Fraud team for help',
    ],
    cannot: [
      'Close a case — a second pair of eyes closes it',
      'Change how the system detects risk',
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
      'Dashboard — waiting cases, time waiting, team workload and results',
    ],
    can: [
      'Everything an analyst can do',
      'Close cases and reassign them',
      'See whether the desk is coping',
    ],
    cannot: [
      'Change how the system detects risk — that is a different job',
    ],
    why: 'They are accountable for whether the queue is being cleared, which is '
       + 'why they get the operations view and nobody else on the floor does.',
  },
  {
    id: 'INFOSEC_ANALYST',
    title: 'Information Security Analyst',
    who: 'Handles suspected account compromise rather than commercial loss.',
    sees: [
      'Cases to review and Find a transaction',
      'Dashboard',
    ],
    can: [
      'Read and investigate any case',
      'Send a case for specialist help',
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
      'System settings — detection rules, limits, system versions and activity records',
      'Dashboard',
      'No access to customer cases',
    ],
    can: [
      'Enable, disable and retune rules',
      'Publish new risk limits',
      'Change or restore a detection-system version',
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

    </>
  )
}
