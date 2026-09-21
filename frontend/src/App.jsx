import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './lib/api'
import Login from './pages/Login'
import Triage from './pages/Triage'
import Operations from './pages/Operations'
import Metrics from './pages/Metrics'
import Transactions from './pages/Transactions'
import Admin from './pages/Admin'
import Roles from './pages/Roles'
import Intake from './pages/Intake'
import Approvals from './pages/Approvals'
import Tracker from './pages/Tracker'

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
    can('cases:read') && { to: '/live', label: 'Live desk', section: 'Surveillance & cases', hint: 'what is arriving, what is waiting, what to expect' },
    can('cases:read') && { to: '/triage', label: 'Case queue', section: 'Surveillance & cases', hint: 'work the queue' },
    can('cases:read') && { to: '/tracker', label: 'Case tracker', section: 'Surveillance & cases', hint: 'every case, from alert to closed' },
    can('cases:approve_fraud') && { to: '/approvals', label: 'Approvals', section: 'Surveillance & cases', hint: "decide other people's fraud findings" },
    can('metrics:read') && { to: '/operations', label: 'Operations', section: 'Intelligence', hint: 'is the desk coping' },
    can('cases:read') && { to: '/transactions', label: 'Search', section: 'Intelligence', hint: 'find a transaction' },
    can('metrics:read') && { to: '/analytics', label: 'Analytics', section: 'Intelligence', hint: 'volume and model' },
    can('admin:rules') && { to: '/admin', label: 'Administration', section: 'Governance', hint: 'rules and thresholds' },
    { to: '/roles', label: 'Who does what', section: 'Governance', hint: 'the four roles, and their limits' },
  ].filter(Boolean)
}

function initials(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((s) => s[0].toUpperCase()).join('')
}

export default function App() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const location = useLocation()

  const refresh = useCallback(async () => {
    try { setUser(await api.me()) } catch { setUser(null) } finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  if (loading) return <div className="login-wrap"><p className="muted">Loading…</p></div>
  if (!user) return <Login onSignedIn={refresh} />

  const links = nav(user.permissions)
  const landing = links.find((l) => l.to === '/triage')?.to ?? links[0]?.to ?? '/triage'
  // Triage manages its own scrolling columns; every other page scrolls normally.
  const flush = location.pathname.startsWith('/triage')

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
          <span className="logo-tile">R</span>
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
              <span className="profile-role">{user.role.replace(/_/g, ' ').toLowerCase()}</span>
            </span>
            <button className="ghost icon-btn" title="Sign out"
                    onClick={async () => { await api.logout(); refresh() }} aria-label="Sign out">
              <Icon paths={['M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4', 'M10 17l5-5-5-5', 'M15 12H3']} />
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="searchbar">
            <Icon paths={['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M20 20l-4-4']} />
            <input type="search" placeholder="Search cases, entities, transactions…"
                   aria-label="Search" />
          </div>
          <div className="topbar-right">
            <span className="live"><span className="live-dot on" /> System status: Nominal</span>
            {/* The system is advisory and says so where an operator can see it. */}
            <span className="pill" title="Risk Intelligence recommends; it does not block, hold or reverse.">Advisory</span>
          </div>
        </header>

        <div className={`content ${flush ? 'flush' : ''}`}
             style={flush ? { flexDirection: 'column' } : undefined}>
          <Routes>
            <Route path="/" element={<Navigate to={landing} replace />} />
            <Route path="/live" element={<Intake />} />
            <Route path="/tracker" element={<Tracker />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/triage" element={<Triage user={user} />} />
            <Route path="/operations" element={<Operations />} />
            <Route path="/transactions" element={<Transactions />} />
            <Route path="/analytics" element={<Metrics />} />
            <Route path="/admin" element={<Admin user={user} />} />
            <Route path="/roles" element={<Roles user={user} />} />
            <Route path="*" element={<Navigate to={landing} replace />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
