import { useState, type FormEvent } from 'react'
import { Check, ChevronRight, LoaderCircle } from 'lucide-react'
import Dialog from './Dialog'
import { type Account, type Settings, phaseLabel } from './types'
import './settings.css'

type Props = {
  saved: Settings
  accounts: Account[]
  account: string
  busy: boolean
  chooseAccount: (account: string) => void
  save: (value: Settings) => Promise<void>
  close: () => void
}

export default function SettingsDialog({ saved, accounts, account, busy, chooseAccount, save, close }: Props) {
  const [value, change] = useState(() => ({ ...saved }))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const disabled = busy || saving
  const dirty = value.identity !== saved.identity || value.idle_minutes !== saved.idle_minutes || value.stop_on_exit !== saved.stop_on_exit

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (disabled || !dirty) return
    setSaving(true)
    setError('')
    try {
      await save(value)
    } catch (error) { setError((error as Error).message) } finally { setSaving(false) }
  }

  return <Dialog title="工作台设置" className="settings-dialog" close={close} busy={disabled}>
    <form onSubmit={submit}>
      <div className="settings-content">
        <section className="settings-accounts" aria-label="本机账号">
          <div className="settings-section-label"><h3>计算账号</h3><span>切换即生效</span></div>
          <div className="settings-account-list">{accounts.map(profile => <button type="button" key={profile.id} className="settings-account" disabled={disabled} aria-label={`切换到 ${profile.id}`} aria-pressed={profile.id === account} onClick={() => chooseAccount(profile.id)}>
            <span className="settings-account-name"><strong>{profile.id}</strong><Check size={13} aria-hidden="true" /></span>
            <span className="settings-account-status"><span className={`tiny-dot ${profile.machine.phase === 'ready' ? '' : 'muted'}`} />{profile.credential_present ? phaseLabel[profile.machine.phase] : '待登录'}</span>
          </button>)}</div>
        </section>

        <div className="settings-lifecycle">
          <label className="setting-row">
            <span className="setting-copy"><strong>空闲自动关闭</strong><small>队列空闲后释放 GPU</small></span>
            <select aria-label="空闲自动关闭" value={value.idle_minutes} disabled={disabled} onChange={e => change({ ...value, idle_minutes: Number(e.target.value) })}>
              {[0, 5, 10, 15, 30, 60, 120].map(n => <option key={n} value={n}>{n ? `${n} 分钟后` : '手动关闭'}</option>)}
            </select>
          </label>
          <label className="setting-row exit-setting">
            <span className="setting-copy"><strong>退出时释放机器</strong><small>退出本地服务时生效</small></span>
            <span className="setting-switch"><input type="checkbox" role="switch" aria-label="退出时释放机器" checked={value.stop_on_exit} disabled={disabled} onChange={e => change({ ...value, stop_on_exit: e.target.checked })} /><span aria-hidden="true" /></span>
          </label>
        </div>
        <details className="settings-advanced">
          <summary><ChevronRight size={13} aria-hidden="true" />高级连接</summary>
          <label className="settings-identity">
            <span>SSH 私钥路径</span>
            <input aria-label="SSH 私钥路径" aria-describedby="identity-help" value={value.identity} disabled={disabled} placeholder="输入私钥文件完整路径" spellCheck={false} autoComplete="off" onChange={e => change({ ...value, identity: e.target.value })} />
            <small id="identity-help">用于连接云端 ComfyUI，通常无需修改。</small>
          </label>
        </details>
        {error && <p className="settings-error" role="alert">{error}</p>}
      </div>

      <footer className="settings-actions">
        <button type="button" className="secondary" disabled={disabled} onClick={close}>取消</button>
        <button type="submit" className="primary" disabled={disabled || !dirty}>{saving && <LoaderCircle size={14} className="spin" />}{saving ? '保存中…' : '保存设置'}</button>
      </footer>
    </form>
  </Dialog>
}
