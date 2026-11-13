import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, Eye, EyeOff, LoaderCircle } from 'lucide-react'
import App from './App'
import './access.css'

type Session = { required: boolean; authenticated: boolean; username: string | null; csrf: string }

export default function Access() {
  const [session, setSession] = useState<Session | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [visible, setVisible] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const load = useCallback(async () => {
    const response = await fetch('/api/auth/session', { cache: 'no-store' })
    if (!response.ok) throw new Error('无法连接工作台，请稍后重试')
    const next: Session = await response.json()
    setSession(next); setConnectionError('')
    history.replaceState(null, '', next.required && !next.authenticated ? '/login' : '/')
    return next
  }, [])
  useEffect(() => {
    let active = true
    load().catch(e => { if (active) setConnectionError(e.message) })
    const expired = () => { setSession(null); setPassword(''); load().catch(e => setConnectionError(e.message)) }
    window.addEventListener('workbench-session-expired', expired)
    return () => { active = false; window.removeEventListener('workbench-session-expired', expired) }
  }, [load])

  async function login(event: React.FormEvent) {
    event.preventDefault()
    if (busy || !session) return
    setBusy(true); setError('')
    try {
      const response = await fetch('/api/auth/login', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': session.csrf },
        body: JSON.stringify({ username: username.trim(), password }) })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '登录失败，请重试')
      setPassword(''); await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function logout() {
    if (!session) return
    const response = await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-Workbench-Token': session.csrf } })
    if (!response.ok && response.status !== 401) throw new Error('退出失败，请重试')
    setSession(null); await load()
  }
  if (session && (!session.required || session.authenticated)) return <App accountName={session.username ?? undefined} signOut={session.required ? logout : undefined} />
  return <main className="access-page">
    <section className="access-card" aria-label="工作台登录">
      <div className="access-eyebrow"><span /> 创作工作台</div>
      <h1>登录工作台</h1><p className="access-description">继续创作与管理模型服务</p>
      {!session ? <div className="access-connecting" role="status">{connectionError ? <><p role="alert">{connectionError}</p><button className="secondary" onClick={() => load().catch(e => setConnectionError(e.message))}>重新连接</button></> : <><LoaderCircle size={18} className="spin" /> 正在连接…</>}</div> : <form onSubmit={login}>
        <label className="access-field"><span>账号</span><input name="username" aria-label="登录账号" autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} onChange={e => setUsername(e.target.value)} disabled={busy} maxLength={100} required autoFocus /></label>
        <label className="access-field"><span>密码</span><div className="access-password"><input name="password" aria-label="登录密码" type={visible ? 'text' : 'password'} autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy} maxLength={1024} required /><button type="button" className="icon-button" aria-label={visible ? '隐藏密码' : '显示密码'} aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>
        {error && <p className="access-error" role="alert">{error}</p>}
        <button className="primary access-submit" disabled={busy || !username.trim() || !password}>{busy ? <><LoaderCircle size={16} className="spin" />登录中…</> : <>登录<ArrowRight size={17} /></>}</button>
      </form>}
      <footer>仅限已授权账号</footer>
    </section>
  </main>
}
