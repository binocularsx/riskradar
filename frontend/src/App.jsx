import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './lib/api'
import Login from './pages/Login'
import Queue from './pages/Queue'
import CaseDetail from './pages/CaseDetail'
import Metrics from './pages/Metrics'
import Transactions from './pages/Transactions'
import Admin from './pages/Admin'

/**
 * Navigation is derived from the permissions the *server* returned, so the menu
 * matches what the API will actually allow. It is presentation only — separation
 * of duties (D12b) is enforced on every route in the backend, and an ADMIN who
 * typed /cases into the address bar gets a 403, not a case.
 */
function nav(permissions) {
  const can = (p) => permissions.includes(p)
  return [
    can('cases:read') && { to: '/queue', label: 'Alert queue' },
    can('cases:read') && { to: '/transactions', label: 'Transactions' },
    can('metrics:read') && { to: '/metrics', label: 'Metrics' },
    can('admin:rules') && { to: '/admin', label: 'Administration' },
  ].filter(Boolean)
}

export default function App() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const location = useLocation()

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (loading) {
    return <div className="login-wrap"><p className="muted">Loading…</p></div>
  }
  if (!user) {
    return <Login onSignedIn={refresh} />
  }

  const links = nav(user.permissions)
  const landing = links[0]?.to ?? '/queue'

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-name">Risk Radar</span>
          <span className="brand-tag">advisory</span>
        </div>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
          >
            {link.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <div style={{ fontSize: 13 }}>{user.display_name}</div>
          <div className="dim" style={{ fontSize: 11, marginBottom: 10 }}>
            {user.role.replace(/_/g, ' ').toLowerCase()}
          </div>
          <button
            style={{ width: '100%' }}
            onClick={async () => {
              await api.logout()
              refresh()
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>{links.find((l) => location.pathname.startsWith(l.to))?.label ?? 'Case'}</h1>
          {/* The system is advisory and says so where an operator can see it,
              not only in the API description (D7). */}
          <span className="dim" style={{ fontSize: 12 }}>
            Advisory only — Risk Radar recommends; it does not block, hold or reverse.
          </span>
        </header>

        <div className="content">
          <Routes>
            <Route path="/" element={<Navigate to={landing} replace />} />
            <Route path="/queue" element={<Queue user={user} />} />
            <Route path="/cases/:id" element={<CaseDetail user={user} />} />
            <Route path="/transactions" element={<Transactions />} />
            <Route path="/metrics" element={<Metrics />} />
            <Route path="/admin" element={<Admin user={user} />} />
            <Route path="*" element={<Navigate to={landing} replace />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
