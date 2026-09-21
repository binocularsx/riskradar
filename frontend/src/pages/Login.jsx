import { useMemo, useRef, useState } from 'react'

import { api } from '../lib/api'

/**
 * Faithful build of the "Risk Intelligence" Figma login (design node 12:5397).
 *
 * Left panel: the real two-step sign-in, wired to the backend. A correct
 * password can return `mfa_required` — not an error: a second factor is
 * mandatory (D12a/D95), and nothing is granted until it is supplied, which is
 * the "Verify your sign-in" state.
 *
 * Right panel: the Real-Time Surveillance Stream showcase. The login page is
 * pre-auth, so it cannot show live data — this is a faithful static reproduction
 * of the design, exactly as the mockup presents it.
 */

const STREAM = [
  { title: 'Velocity Spike Post-Password Reset', tag: 'CRITICAL', tone: 'critical',
    txn: 'TXN-9042', amount: '₦4,850,000', rule: 'RULE-SEC-04', score: 94, status: 'Hold Triggered' },
  { title: 'Cross-Border Corporate Settlement', tag: 'Cleared', tone: 'ok',
    txn: 'TXN-8812', amount: '$124,500.00', rule: 'Allow List Validated', score: 12, status: 'Auto-Approved' },
  { title: 'Dormant Account Reactivation Spurt', tag: 'MEDIUM', tone: 'medium',
    txn: 'TXN-7640', amount: '₦750,000', rule: 'RULE-DOR-01', score: 68, status: 'In Review Queue' },
]

// Inference-traffic bars; index 12 is the isolated anomaly (highlighted).
const BARS = [22, 30, 26, 34, 28, 38, 33, 44, 40, 52, 46, 58, 96, 54, 49, 60, 52, 63, 55, 48, 40, 34, 30, 26]
const ANOMALY = 12

export default function Login({ onSignedIn }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const [code, setCode] = useState(['', '', '', '', '', ''])
  const [needsTotp, setNeedsTotp] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const boxes = useRef([])

  const now = useMemo(() => new Date().toLocaleTimeString('en-GB',
    { hour: '2-digit', minute: '2-digit' }), [])

  async function signIn(event) {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await api.login(email, password, null)
      if (result.status === 'mfa_required') setNeedsTotp(true)
      else onSignedIn()
    } catch {
      // One message for every failure mode, so login cannot enumerate accounts.
      setError('Sign-in failed. Check your details and try again.')
    } finally { setBusy(false) }
  }

  async function verify(event) {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await api.login(email, password, code.join(''))
      if (result.status === 'mfa_required') setError('That code was not accepted. Try the next one.')
      else onSignedIn()
    } catch {
      setError('That code was not accepted. Try the next one.')
    } finally { setBusy(false) }
  }

  function onCodeChange(i, value) {
    const digits = value.replace(/\D/g, '')
    if (!digits) { setCode((c) => c.map((v, j) => (j === i ? '' : v))); return }
    setCode((c) => {
      const next = [...c]
      // Support pasting the whole code into one box.
      for (let k = 0; k < digits.length && i + k < 6; k++) next[i + k] = digits[k]
      return next
    })
    const target = Math.min(i + digits.length, 5)
    boxes.current[target]?.focus()
  }
  function onCodeKey(i, e) {
    if (e.key === 'Backspace' && !code[i] && i > 0) boxes.current[i - 1]?.focus()
  }

  const unavailable = (what) => (e) => {
    e.preventDefault()
    setNotice(`${what} is not configured in this environment — use email and password.`)
  }

  return (
    <div className="auth">
      <header className="auth-top">
        <div className="auth-brand">
          <span className="logo-tile">R</span>
          <span className="auth-word">Risk Intelligence</span>
          <span className="auth-inst">INSTITUTIONAL</span>
        </div>
        <div className="auth-status">
          <span className="live"><span className="live-dot on" /> Auth Node: Active</span>
          <span className="dim mono">v4.2.8-prod</span>
          <span className="dim mono">{now}</span>
        </div>
      </header>

      <main className="auth-body">
        {/* ---------------------------------------------- left: sign in */}
        <section className="auth-panel auth-signin">
          {!needsTotp ? (
            <form onSubmit={signIn}>
              <h1 className="auth-h1">Welcome to Risk Intelligence</h1>
              <p className="auth-sub">Sign in to continue</p>

              {error && <div className="auth-error">{error}</div>}
              {notice && <div className="auth-note">{notice}</div>}

              <div className="field">
                <label htmlFor="email">Email address</label>
                <input id="email" type="email" autoComplete="username" placeholder="m.vance@riskintel.io"
                       value={email} onChange={(e) => setEmail(e.target.value)} required />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <input id="password" type="password" autoComplete="current-password"
                       value={password} onChange={(e) => setPassword(e.target.value)} required />
              </div>

              <div className="auth-row">
                <label className="checkline">
                  <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                  Remember me
                </label>
                <a href="#" onClick={unavailable('Password recovery')}>Forgot password?</a>
              </div>

              <button className="primary auth-submit" type="submit" disabled={busy}>
                {busy ? 'Signing in…' : 'Sign In'} <span className="arr">→</span>
              </button>

              <div className="auth-or"><span>OR</span></div>
              <button type="button" className="auth-sso" onClick={unavailable('Microsoft SSO')}>
                <span className="ms-glyph" aria-hidden="true"><i /><i /><i /><i /></span>
                Sign in with Microsoft
              </button>

              <p className="auth-legal">
                All authentication attempts, device fingerprints and session telemetry are logged in
                compliance with AML Tier 1 standards.
              </p>
            </form>
          ) : (
            <form onSubmit={verify}>
              <h1 className="auth-h1">Verify your sign-in</h1>
              <p className="auth-sub">Enter the 6-digit verification code to continue.</p>

              {error && <div className="auth-error">{error}</div>}

              <div className="field">
                <label>Security code</label>
                <div className="code-boxes">
                  {code.map((digit, i) => (
                    <input key={i} ref={(el) => (boxes.current[i] = el)} className="code-box"
                           inputMode="numeric" maxLength={6} autoComplete="one-time-code" autoFocus={i === 0}
                           value={digit} onChange={(e) => onCodeChange(i, e.target.value)}
                           onKeyDown={(e) => onCodeKey(i, e)} />
                  ))}
                </div>
              </div>

              <button className="primary auth-submit" type="submit" disabled={busy || code.join('').length < 6}>
                {busy ? 'Verifying…' : 'Verify'} <span className="arr">→</span>
              </button>
              <button type="button" className="ghost auth-back"
                      onClick={() => { setNeedsTotp(false); setCode(['', '', '', '', '', '']); setError(null) }}>
                ← Back to Sign In
              </button>

              <p className="auth-legal">
                Sessions are held server-side and can be revoked immediately. This console shows financial
                data; it never stores a token in the browser.
              </p>
            </form>
          )}
        </section>

        {/* ------------------------------------- right: surveillance stream */}
        <section className="auth-panel auth-stream">
          <div className="stream-head">
            <div>
              <div className="stream-title">
                <span className="live-dot on" /> Real-Time Surveillance Stream
                <span className="pill" style={{ marginLeft: 8 }}>#SURV-88</span>
              </div>
              <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>Evaluation: 14.2k/sec</div>
            </div>
            <span className="live"><span className="live-dot on" /> Live</span>
          </div>

          <div className="stream-stats">
            {[['Scoring Engine', 'Sentinel v4.2', '99.8% nominal'],
              ['P99 Latency', '42 ms', 'Sub-threshold'],
              ['Auto-Suppression', '96.4%', 'Zero friction']].map(([k, v, n]) => (
              <div className="stream-stat" key={k}>
                <div className="k">{k}</div>
                <div className="v">{v}</div>
                <div className="n">{n}</div>
              </div>
            ))}
          </div>

          <div className="stream-list">
            {STREAM.map((a) => (
              <div className={`stream-row ${a.tone}`} key={a.txn}>
                <div className="stream-row-main">
                  <div className="stream-row-title">
                    <span className={`risk risk-${a.tone === 'critical' ? 'CRITICAL' : a.tone === 'medium' ? 'MEDIUM' : 'LOW'}`}>{a.tag}</span>
                    {a.title}
                  </div>
                  <div className="stream-row-meta mono dim">
                    {a.txn} · {a.amount} · {a.rule}
                  </div>
                </div>
                <div className="stream-row-score">
                  <div className="score">Score: <strong>{a.score}</strong></div>
                  <div className="dim" style={{ fontSize: 11 }}>{a.status}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="stream-chart">
            <div className="between" style={{ marginBottom: 8 }}>
              <span className="h3-inline">Inference Traffic Distribution</span>
              <span className="dim mono" style={{ fontSize: 11 }}>24h Peak: 24,190/m</span>
            </div>
            <div className="tdist">
              {BARS.map((h, i) => (
                <span key={i} className={`tbar ${i === ANOMALY ? 'anomaly' : ''}`} style={{ height: `${h}%` }} />
              ))}
            </div>
            <div className="tdist-axis dim mono">
              <span>00:00</span><span>06:00</span><span>12:00 (Anomaly isolated)</span><span>18:00</span>
            </div>
          </div>
        </section>
      </main>

      <footer className="auth-foot">
        <span>© 2026 Risk Intelligence Inc.</span>
        <span className="auth-foot-links">
          <a href="#" onClick={(e) => e.preventDefault()}>Privacy &amp; Data Governance</a>
          <a href="#" onClick={(e) => e.preventDefault()}>Security Center</a>
        </span>
        <span className="dim">SOC 2 Type II Certified • FIPS 140-3 Hardware Level</span>
      </footer>
    </div>
  )
}
