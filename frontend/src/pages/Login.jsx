import { useState } from 'react'

import { api } from '../lib/api'
import { Banner } from '../components/ui'

/**
 * Two-step sign-in.
 *
 * The password step can return `mfa_required` — correct password, no session
 * yet. That is not an error state: for FRAUD_OPS_LEAD and ADMIN a second factor
 * is mandatory (D12a), and nothing is granted until it is supplied.
 */
export default function Login({ onSignedIn }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [needsTotp, setNeedsTotp] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const result = await api.login(email, password, needsTotp ? totp : null)
      if (result.status === 'mfa_required') {
        setNeedsTotp(true)
      } else {
        onSignedIn()
      }
    } catch (err) {
      // The server returns one message for every failure mode so login cannot be
      // used to enumerate accounts; the UI must not helpfully undo that.
      setError('Sign-in failed. Check your details and try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand" style={{ padding: '0 0 16px' }}>
          <span className="brand-name">Risk Radar</span>
          <span className="brand-tag">fraud operations</span>
        </div>

        {error && <Banner kind="error">{error}</Banner>}

        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>

        {needsTotp && (
          <div className="field">
            <label htmlFor="totp">Authenticator code</label>
            <input
              id="totp"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="000000"
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
              autoFocus
              required
            />
          </div>
        )}

        <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
          {busy ? 'Signing in…' : needsTotp ? 'Verify and sign in' : 'Sign in'}
        </button>

        <p className="dim" style={{ fontSize: 11, marginTop: 16, marginBottom: 0 }}>
          Sessions are held server-side and can be revoked immediately. This console
          shows financial data; it never stores a token in the browser.
        </p>
      </form>
    </div>
  )
}
