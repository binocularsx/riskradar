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
import Tracker from './pages/Tracker'

/**
 * Navigation is built from the permissions the *server* returned, so the menu
 * matches what the API will actually allow. It is presentation only —
 * separation of duties is enforced on every route in the backend, and an
 * administrator who types /triage into the address bar gets a 403, not a case.
 */
function nav(permissions) {
  const can = (p) => permissions.includes(p)
  return [
    can('cases:read') && { to: '/live', label: 'Live desk', hint: 'what is arriving, what is waiting, what to expect' },
    can('cases:read') && { to: '/triage', label: 'Triage', hint: 'work the queue' },
    can('cases:read') && { to: '/tracker', label: 'Case tracker', hint: 'every case, from alert to closed' },
    can('metrics:read') && { to: '/operations', label: 'Operations', hint: 'is the desk coping' },
    can('cases:read') && { to: '/transactions', label: 'Search', hint: 'find a transaction' },
    can('metrics:read') && { to: '/analytics', label: 'Analytics', hint: 'volume and model' },
    can('admin:rules') && { to: '/admin', label: 'Administration', hint: 'rules and thresholds' },
    // Visible to every role on purpose: a control people cannot see is a
    // control they will work around.
    { to: '/roles', label: 'Who does what', hint: 'the four roles, and their limits' },
  ].filter(Boolean)
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
  const active = links.find((l) => location.pathname.startsWith(l.to))
  // Triage manages its own scrolling columns; every other page scrolls normally.
  const flush = location.pathname.startsWith('/triage')

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-name">Risk Radar</span>
          <span className="brand-tag">fraud operations</span>
        </div>
        {links.map((link) => (
          <NavLink key={link.to} to={link.to}
                   className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            {link.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <div style={{ fontSize: 13 }}>{user.display_name}</div>
          <div className="dim" style={{ fontSize: 11, marginBottom: 10 }}>
            {user.role.replace(/_/g, ' ').toLowerCase()}
          </div>
          <button style={{ width: '100%', justifyContent: 'center' }}
                  onClick={async () => { await api.logout(); refresh() }}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <h1>{active?.label ?? 'Risk Radar'}</h1>
            {active?.hint && (
              <span className="dim" style={{ fontSize: 11.5 }}>{active.hint}</span>
            )}
          </div>
          {/* The system is advisory and says so where an operator can see it,
              not only in the API documentation. */}
          <span className="dim" style={{ fontSize: 11.5, textAlign: 'right' }}>
            Advisory — Risk Radar recommends.<br />It does not block, hold or reverse.
          </span>
        </header>

        <div className={`content ${flush ? 'flush' : ''}`}
             style={flush ? { flexDirection: 'column' } : undefined}>
          <Routes>
            <Route path="/" element={<Navigate to={landing} replace />} />
            <Route path="/live" element={<Intake />} />
            <Route path="/tracker" element={<Tracker />} />
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
