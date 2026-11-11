import { useState } from 'react'
import { Check, ChevronRight, LoaderCircle, Settings2 } from 'lucide-react'
import { type Account, type Settings, phaseLabel } from './types'
import './settings.css'

type Props = {
  value: Settings
  saved: Settings
  accounts: Account[]
  account: string
  busy: boolean
  saving: boolean
  change: (value: Settings) => void
  chooseAccount: (account: string) => void
  save: () => void
}

export default function SettingsPanel({ value, saved, accounts, account, busy, saving, change, chooseAccount, save }: Props) {
  const [section, setSection] = useState<'general' | 'accounts'>('general')
  const dirty = value.identity !== saved.identity || value.idle_minutes !== saved.idle_minutes || value.stop_on_exit !== saved.stop_on_exit

  return <section className="settings-workspace" aria-label="工作台配置">
    <div className="settings-surface panel">
    <nav className="settings-tabs" aria-label="设置分类">
      <button aria-pressed={section === 'general'} onClick={() => setSection('general')}>常规设置</button>
      <button aria-pressed={section === 'accounts'} onClick={() => setSection('accounts')}>本机账号 <span>{accounts.length}</span></button>
    </nav>

    <div className={`settings-content settings-view-${section}`}>
      <section className="settings-general">
        <header className="settings-card-heading"><h2><Settings2 size={15} />连接与关机</h2></header>
        <div className="settings-fields">
          <label className="settings-identity">
            <span>SSH 私钥路径</span>
            <input aria-label="SSH 私钥路径" aria-describedby="identity-help" value={value.identity} disabled={busy} placeholder="输入私钥文件完整路径" spellCheck={false} autoComplete="off" onChange={e => change({ ...value, identity: e.target.value })} />
            <small id="identity-help">仅保存本机路径，用于连接云端机器。</small>
          </label>

          <div className="settings-lifecycle">
            <label className="setting-row idle-setting">
              <span className="setting-copy"><strong>空闲自动关闭</strong><small>云端队列空闲后释放机器</small></span>
              <select aria-label="空闲自动关闭" value={value.idle_minutes} disabled={busy} onChange={e => change({ ...value, idle_minutes: Number(e.target.value) })}>
                {[0, 5, 10, 15, 30, 60, 120].map(n => <option key={n} value={n}>{n ? `${n} 分钟后` : '手动关闭'}</option>)}
              </select>
            </label>
            <label className="setting-row exit-setting">
              <span className="setting-copy"><strong>退出时释放机器</strong><small>正常退出本地服务时生效</small></span>
              <span className="setting-switch"><input type="checkbox" role="switch" aria-label="退出时释放机器" checked={value.stop_on_exit} disabled={busy} onChange={e => change({ ...value, stop_on_exit: e.target.checked })} /><span aria-hidden="true" /></span>
            </label>
          </div>

          <details className="settings-details"><summary><ChevronRight size={13} />关机与重连说明</summary><p>关闭浏览器不会退出本地服务。自动关机需要本地服务持续运行；断电、断网或强制退出后，请重新核对机器状态。释放 GPU 会清除云端临时模型，下次启动需重新部署。</p></details>
        </div>
      </section>

      <section className="settings-accounts">
        <header className="settings-card-heading"><h2>本机账号</h2><span className="settings-count">{accounts.length}</span></header>
        <div className="settings-account-list">{accounts.map(profile => <button key={profile.id} className={`settings-account ${profile.id === account ? 'active' : ''}`} aria-label={`切换到 ${profile.id}`} aria-pressed={profile.id === account} onClick={() => chooseAccount(profile.id)}>
          <span className="settings-account-copy"><strong>{profile.id}{profile.id === account && <span className="current-account-tag">当前</span>}</strong><small>{profile.credential_present ? '凭证已配置' : '待登录'}</small></span>
          <span className={`status-chip ${profile.machine.phase === 'ready' ? 'success' : ''}`}>{phaseLabel[profile.machine.phase]}</span>
          <ChevronRight size={13} aria-hidden="true" />
        </button>)}</div>
        <p className="settings-account-note">切换仅影响新任务，已有任务继续运行。</p>
      </section>
    </div>

    <footer className="settings-savebar">
      <span className={`settings-save-status ${dirty ? 'unsaved' : ''}`} role="status">{saving ? <LoaderCircle size={13} className="spin" /> : dirty ? <span className="tiny-dot" /> : <Check size={13} />}{saving ? '正在保存…' : dirty ? '有未保存的更改' : '已保存到本机'}</span>
      <div className="settings-save-actions"><button className="secondary" disabled={busy || !dirty} onClick={() => change({ ...saved })}>放弃修改</button><button className="primary" disabled={busy || !dirty} onClick={save}>{saving ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}保存设置</button></div>
    </footer>
    </div>
  </section>
}
