import { useRef, useState } from 'react'

import { api } from '../lib/api'

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M12 3 5 6v5c0 4.6 2.8 8 7 10 4.2-2 7-5.4 7-10V6l-7-3Z" />
      <path d="m9.5 12 1.6 1.6 3.6-3.8" />
    </svg>
  )
}

function EyeIcon({ open }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />
      <circle cx="12" cy="12" r="2.5" />
      {!open && <path d="m4 4 16 16" />}
    </svg>
  )
}

function Signal({ tone, icon, title, detail, status }) {
  return (
    <div className="auth-signal">
      <span className={`auth-signal-icon ${tone}`}>{icon}</span>
      <span className="auth-signal-copy">
        <strong>{title}</strong>
        <span>{detail}</span>
      </span>
      <span className={`auth-signal-status ${tone}`}>{status}</span>
    </div>
  )
}

// Telling the two apart matters: when the service cannot be reached, saying
// "check your details" sends somebody off to re-type a password that was
// never wrong. A 401 is the server deliberately refusing the credentials;
// anything else here means it never gave a real answer at all — it is down,
// unreachable, or something in front of it replied with a page instead.
function unreachable(error) {
  if (error?.unauthenticated) return false
  if (typeof error?.status === 'number' && error.status < 500) return false
  return true
}

const OFFLINE = 'We could not reach Risk Radar. The service may not be running — try again in a moment.'

export default function Login({ onSignedIn }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [remember, setRemember] = useState(true)
  const [code, setCode] = useState(['', '', '', '', '', ''])
  const [needsTotp, setNeedsTotp] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const boxes = useRef([])

  async function signIn(event) {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await api.login(email, password, null)
      if (result.status === 'mfa_required') setNeedsTotp(true)
      else onSignedIn()
    } catch (error) {
      setError(unreachable(error) ? OFFLINE : 'Sign-in failed. Check your details and try again.')
    } finally { setBusy(false) }
  }

  async function verify(event) {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await api.login(email, password, code.join(''))
      if (result.status === 'mfa_required') setError('That code was not accepted. Try the next one.')
      else onSignedIn()
    } catch (error) {
      setError(unreachable(error) ? OFFLINE : 'That code was not accepted. Try the next one.')
    } finally { setBusy(false) }
  }

  function onCodeChange(index, value) {
    const digits = value.replace(/\D/g, '')
    if (!digits) { setCode((current) => current.map((digit, i) => (i === index ? '' : digit))); return }
    setCode((current) => {
      const next = [...current]
      for (let i = 0; i < digits.length && index + i < 6; i++) next[index + i] = digits[i]
      return next
    })
    boxes.current[Math.min(index + digits.length, 5)]?.focus()
  }

  function onCodeKey(index, event) {
    if (event.key === 'Backspace' && !code[index] && index > 0) boxes.current[index - 1]?.focus()
  }

  const unavailable = (what) => (event) => {
    event.preventDefault()
    setNotice(`${what} is not configured in this environment — use email and password.`)
  }

  return (
    <main className="auth">
      <section className="auth-login-side">
        <div className="auth-form-shell">
          <div className="auth-brand-row">
            <img className="auth-product-logo" src="/assets/risk-radar-logo.png" alt="Risk Radar" />
            <span className="auth-brand-rule" />
            <span className="auth-partner-copy">Institutional platform for</span>
            <img className="auth-gtco-logo" src="/assets/gtco-logo.webp" alt="GTCO" />
          </div>
          {!needsTotp ? (
            <form className="auth-form" onSubmit={signIn}>
              <h1>Welcome to Risk Intelligence</h1>
              <p className="auth-intro">Sign into your account</p>

              {error && <div className="auth-error">{error}</div>}
              {notice && <div className="auth-note">{notice}</div>}

              <div className="auth-field">
                <label htmlFor="email">Email</label>
                <input id="email" type="email" autoComplete="username"
                       value={email} onChange={(event) => setEmail(event.target.value)} required />
              </div>

              <div className="auth-field">
                <label htmlFor="password">Password</label>
                <div className="auth-password">
                  <input id="password" type={showPassword ? 'text' : 'password'} autoComplete="current-password"
                         value={password} onChange={(event) => setPassword(event.target.value)} required />
                  <button type="button" className="auth-eye" aria-label={showPassword ? 'Hide password' : 'Show password'}
                          onClick={() => setShowPassword((value) => !value)}>
                    <EyeIcon open={showPassword} />
                  </button>
                </div>
              </div>

              <div className="auth-options">
                <label className="auth-remember">
                  <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />
                  <span>Remember Me</span>
                </label>
                <a href="#" onClick={unavailable('Password recovery')}>Forgot Password?</a>
              </div>

              <button className="auth-login-button" type="submit" disabled={busy}>
                {busy ? 'Signing in…' : 'Log In'}
              </button>

              <div className="auth-divider"><span>OR</span></div>
              <button type="button" className="auth-microsoft" onClick={unavailable('Microsoft SSO')}>
                <span className="ms-glyph" aria-hidden="true"><i /><i /><i /><i /></span>
                Sign in with Microsoft
              </button>

              <p className="auth-signup">Don’t have any account? <a href="#" onClick={unavailable('Account registration')}>Sign Up</a></p>
            </form>
          ) : (
            <form className="auth-form auth-mfa-form" onSubmit={verify}>
              <h1>Verify your sign-in</h1>
              <p className="auth-intro">Enter the 6-digit verification code to continue.</p>
              {error && <div className="auth-error">{error}</div>}

              <div className="auth-field">
                <label>Security code</label>
                <div className="code-boxes">
                  {code.map((digit, index) => (
                    <input key={index} ref={(element) => (boxes.current[index] = element)} className="code-box"
                           inputMode="numeric" maxLength={6} autoComplete="one-time-code" autoFocus={index === 0}
                           value={digit} onChange={(event) => onCodeChange(index, event.target.value)}
                           onKeyDown={(event) => onCodeKey(index, event)} />
                  ))}
                </div>
              </div>

              <button className="auth-login-button" type="submit" disabled={busy || code.join('').length < 6}>
                {busy ? 'Verifying…' : 'Verify'}
              </button>
              <button type="button" className="auth-back"
                      onClick={() => { setNeedsTotp(false); setCode(['', '', '', '', '', '']); setError(null) }}>
                ← Back to Log In
              </button>
            </form>
          )}
        </div>

      </section>

      <section className="auth-showcase" aria-label="Risk Intelligence platform overview">
        <div className="auth-engine-card">
          <div className="auth-engine-head">
            <span className="auth-engine-icon"><ShieldIcon /></span>
            <span className="auth-engine-name">
              <strong>Risk Intelligence Engine</strong>
              <small>Real-time detection &amp; institutional oversight</small>
            </span>
            <span className="auth-active"><i /> Active</span>
          </div>

          <div className="auth-queue-row">
            <span>28 Active Cases in Queue</span>
            <span className="auth-clear">✓&nbsp;&nbsp; 0 Unhandled Critical Alerts</span>
          </div>

          <div className="auth-engine-stats">
            <div><span>Decision Accuracy</span><strong>99.4% <em>+0.2%</em></strong></div>
            <div><span>Average review time</span><strong>4.2m <em>on time</em></strong></div>
          </div>

          <h3>Recent signals evaluated</h3>
          <div className="auth-signals">
            <Signal tone="danger" icon="△" title="Simulated Account Takeover"
                    detail="Rule Engine • High Risk Pattern" status="Flagged & Held" />
            <Signal tone="success" icon="✓" title="Authorized Multi-Factor Check"
                    detail="Institutional MFA Token • Tier-1 Pass" status="Verified" />
          </div>

          <div className="auth-engine-foot">
            <span>Risk monitoring is active across all connected locations.</span>
            <strong>PROTECTED</strong>
          </div>
        </div>

        <div className="auth-showcase-copy">
          <h2>Detect suspicious activity earlier.</h2>
          <p>Monitor transactions, investigate cases, and make informed<br className="desktop-only" /> decisions from one unified, enterprise-grade workspace.</p>
          <div className="auth-capabilities">
            <span><ShieldIcon /> Hardware-Isolated MFA</span>
            <span><ShieldIcon /> End-to-End Field Encryption</span>
            <span>ϟ&nbsp; &lt;12ms Engine Latency</span>
          </div>
        </div>
      </section>
    </main>
  )
}
