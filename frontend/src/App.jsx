import { useCallback, useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { api } from './lib/api'
import Login from './pages/Login'
import CaseQueue from './pages/CaseQueue'
import CaseDetail from './pages/CaseDetail'
import Operations from './pages/Operations'
import Metrics from './pages/Metrics'
import Transactions from './pages/Transactions'
import Admin from './pages/Admin'
import Roles from './pages/Roles'
import Intake from './pages/Intake'
import Approvals from './pages/Approvals'
import CompletedCases from './pages/CompletedCases'
import Tracker from './pages/Tracker'
import { ROLE, say } from './lib/words'

/* Line icons for the nav, inline so they inherit currentColor and need no
 * fetch. Generic UI glyphs, not brand marks. */
function Icon({ d, paths }) {
  return (
    <svg className="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths ? paths.map((p, i) => <path key={i} d={p} />) : <path d={d} />}
    </svg>
  )
}
const ICONS = {
  '/live': ['M3 12h4l2 5 4-13 2 8h6'],
  '/triage': ['M4 5h16', 'M4 12h16', 'M4 19h10'],
  '/tracker': ['M4 4h5v16H4z', 'M10 4h5v10h-5z', 'M16 4h4v7h-4z'],
  '/approvals': ['M20 6 9 17l-5-5'],
  '/completed': ['M4 5h16v4H4z', 'M6 9v10h12V9', 'M10 13h4'],
  '/operations': ['M12 3a9 9 0 1 0 9 9', 'M12 12l5-3'],
  '/transactions': ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M20 20l-4-4'],
  '/analytics': ['M4 20V10', 'M10 20V4', 'M16 20v-7', 'M22 20H2'],
  '/admin': ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3',
             'M1 14h6', 'M9 8h6', 'M17 16h6'],
  '/roles': ['M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M3 20a6 6 0 0 1 12 0', 'M17 11l2 2 4-4'],
}

/**
 * Navigation is built from the permissions the *server* returned, so the menu
 * matches what the API will actually allow. It is presentation only —
 * separation of duties is enforced on every route in the backend, and an
 * administrator who types /triage into the address bar gets a 403, not a case.
 */
function nav(permissions) {
  const can = (p) => permissions.includes(p)
  return [
    can('cases:read') && { to: '/live', label: 'New activity', section: 'Cases', hint: 'See new transactions and alerts' },
    can('cases:read') && { to: '/triage', label: 'Cases to review', section: 'Cases', hint: 'Review cases that need attention' },
    can('cases:read') && { to: '/tracker', label: 'Progress board', section: 'Cases', hint: 'Every open case, laid out by stage' },
    can('cases:read') && { to: '/transactions', label: 'Payment lookup', section: 'Cases', hint: 'Find any payment and see how it was checked' },
    can('cases:approve_fraud') && { to: '/approvals', label: 'Decisions to approve', section: 'Cases', hint: 'Check and approve investigation results' },
    can('cases:close') && { to: '/completed', label: 'Completed cases', section: 'Cases', hint: 'Every closed case and how it was decided' },
    can('metrics:read') && { to: '/operations', label: 'Operations', section: 'Reports', hint: 'See service readiness, urgent work and team workload' },
    can('metrics:read') && { to: '/analytics', label: 'Performance reports', section: 'Reports', hint: 'See transaction and detection trends' },
    can('admin:rules') && { to: '/admin', label: 'System settings', section: 'Settings', hint: 'Manage detection rules and limits' },
    { to: '/roles', label: 'Access and roles', section: 'Settings', hint: 'See what each team member can do' },
  ].filter(Boolean)
}

function initials(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((s) => s[0].toUpperCase()).join('')
}

export default function App() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [logoutError, setLogoutError] = useState('')

  const refresh = useCallback(async () => {
    try { setUser(await api.me()) } catch { setUser(null) } finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    const requireAuthentication = () => setUser(null)
    window.addEventListener('riskradar:auth-required', requireAuthentication)
    return () => window.removeEventListener('riskradar:auth-required', requireAuthentication)
  }, [])

  if (loading) return <div className="login-wrap"><p className="muted">Loading…</p></div>
  if (!user) return <Login onSignedIn={refresh} />

  const links = nav(user.permissions)
  // A supervisor's home is oversight, not the analyst work queue. cases:close
  // is what separates the two jobs: the lead watches the desk, the analyst
  // works it. Landing a lead on /triage framed the role as a queue worker.
  const supervises = user.permissions.includes('cases:close')
  const home = supervises ? '/operations' : '/triage'
  const landing = links.find((l) => l.to === home)?.to
    ?? links.find((l) => l.to === '/triage')?.to ?? links[0]?.to ?? '/triage'
  const guard = (permission, element) => user.permissions.includes(permission) ? element
    : <div className="card"><h1>Access restricted</h1><p>Your role cannot open this page.</p><Link to={landing}>Return to your workspace</Link></div>

  // Nav grouped under its section headers, order preserved.
  const sections = []
  for (const link of links) {
    let group = sections.find((s) => s.name === link.section)
    if (!group) { group = { name: link.section, items: [] }; sections.push(group) }
    group.items.push(link)
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-logo" src="/assets/risk-radar-logo.png" alt="" />
          <span className="brand-text">
            <span className="brand-name">Risk Intelligence</span>
            <span className="brand-tag">Institutional</span>
          </span>
        </div>
        <nav className="nav">
          {sections.map((group) => (
            <div className="nav-group" key={group.name}>
              <div className="nav-section">{group.name}</div>
              {group.items.map((link) => (
                <NavLink key={link.to} to={link.to} title={link.hint}
                         className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
                  <Icon paths={ICONS[link.to]} />
                  <span>{link.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="profile">
            <span className="avatar">{initials(user.display_name)}</span>
            <span className="profile-text">
              <span className="profile-name">{user.display_name}</span>
              <span className="profile-role">{say(ROLE, user.role)}</span>
            </span>
            <button className="ghost icon-btn" title="Sign out"
                    onClick={async () => { try { await api.logout(); setUser(null); setLogoutError('') } catch (e) { setLogoutError(e.message) } }} aria-label="Sign out">
              <Icon paths={['M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4', 'M10 17l5-5-5-5', 'M15 12H3']} />
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          {user.permissions.includes('cases:read') ? <Link className="topbar-workspace-link" to="/transactions">
            <Icon paths={['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M20 20l-4-4']} /> Look up a payment
          </Link> : <span className="muted">Technology operations</span>}
          <div className="topbar-right">
            {user.permissions.includes('cases:approve_fraud') && <Link to="/approvals">Pending decisions</Link>}
            <div className="topbar-profile">
              <span className="avatar">{initials(user.display_name)}</span>
              <span className="topbar-profile-copy">
                <strong>{user.display_name}</strong>
                <small>{say(ROLE, user.role)}</small>
              </span>
            </div>
          </div>
        </header>

        <div className="content">
          {logoutError && <div className="banner error" role="alert">Sign out failed: {logoutError}. Try again.</div>}
          <Routes>
            <Route path="/" element={<Navigate to={landing} replace />} />
            <Route path="/live" element={guard('cases:read', <Intake />)} />
            <Route path="/tracker" element={guard('cases:read', <Tracker />)} />
            <Route path="/approvals" element={guard('cases:approve_fraud', <Approvals />)} />
            <Route path="/completed" element={guard('cases:close', <CompletedCases />)} />
            <Route path="/triage" element={guard('cases:read', <CaseQueue user={user} />)} />
            <Route path="/cases/:id" element={guard('cases:read', <CaseDetail user={user} />)} />
            <Route path="/operations" element={guard('metrics:read', <Operations user={user} />)} />
            <Route path="/transactions" element={guard('cases:read', <Transactions />)} />
            <Route path="/analytics" element={guard('metrics:read', <Metrics user={user} />)} />
            <Route path="/admin" element={guard('admin:rules', <Admin user={user} />)} />
            <Route path="/roles" element={<Roles user={user} />} />
            <Route path="*" element={<Navigate to={landing} replace />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
