import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowUpRight, Check, CircleHelp, Cloud, Cpu, Download, ExternalLink, FileJson, Film, FolderOpen, LayoutGrid, LoaderCircle, MoreHorizontal, Network, Play, Plus, Power, RefreshCw, Save, Settings2, SlidersHorizontal, Sparkles, Terminal, Upload, X } from 'lucide-react'
import Workflow from './Workflow'
import Library from './Library'
import Performance from './Performance'
import Dialog from './Dialog'
import SettingsDialog from './SettingsDialog'
import Resolution, { validDimension, resolutionError } from './Resolution'
import { type State, type Draft, type Job, type Settings, type Graph, activeStatuses, phaseLabel, statusLabel } from './types'

let csrf = ''
async function api<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? response.statusText)) }
  return response.json()
}
const when = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const savedDraft = (draft: Draft): Draft => ({ ...draft, name: draft.name.trim() || '未命名草稿' })
const getNode = (graph: Graph | undefined, type: string) => Object.values(graph ?? {}).find(n => n.class_type === type)

export default function App() {
  const [connected, setConnected] = useState(false)
  const [state, setState] = useState<State | null>(null)
  const [account, setAccount] = useState(() => localStorage.getItem('fieldwork-account') || 'colab2')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [page, setPage] = useState<'studio' | 'library'>('studio')
  const [mobilePanel, setMobilePanel] = useState<'stage' | 'editor' | 'compute'>('stage')
  const [view, setView] = useState<'preview' | 'workflow'>('preview')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saveState, setSaveState] = useState('新建创作')
  const [busy, setBusy] = useState('')
  const [stopAfter, setStopAfter] = useState(false)
  const [modal, setModal] = useState<'json' | 'logs' | 'drafts' | 'help' | 'settings' | null>(null)
  const [json, setJson] = useState('')
  const [logs, setLogs] = useState<{ time: number; message: string }[]>([])
  const [selectedJob, selectJob] = useState<string | null>(null)
  const initialDraft = useRef<Draft | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const known = useRef(new Set<string>())
  const draftRevision = useRef(0)
  const refresh = useCallback(async () => {
    const next = await api<State>('/state'); csrf = next.token; setState(next); setConnected(true); setError(previous => previous.startsWith('本地服务连接中断') ? '' : previous)
    return next
  }, [])

  useEffect(() => {
    let alive = true
    refresh().then(async () => {
      if (!alive) return
      const template = await api<Draft>('/template')
      const graph = structuredClone(template.graph)
      const input = getNode(graph, 'MiniMaxH3ImageToVideo')
      if (input) input.inputs.prompt = ''
      const noise = getNode(graph, 'RandomNoise')
      if (noise) noise.inputs.noise_seed = Math.floor(Math.random() * 2 ** 32)
      if (alive) {
        const fresh = { name: '', graph, positions: {} }
        initialDraft.current = fresh
        setDraft(fresh)
      }
    }).catch(e => setError(String(e.message)))
    const timer = window.setInterval(() => refresh().catch(() => { setConnected(false); setError('本地服务连接中断，请确认启动窗口仍在运行。') }), 2500)
    return () => { alive = false; clearInterval(timer) }
  }, [refresh])

  useEffect(() => {
    if (!state || known.current.has(account)) return
    known.current.add(account)
    api(`/accounts/${account}/refresh`, 'POST').then(refresh).catch(() => refresh())
  }, [account, state, refresh])

  useEffect(() => {
    if (!draft || draft === initialDraft.current) return
    const input = getNode(draft.graph, 'MiniMaxH3ImageToVideo')
    if (input && (!validDimension(Number(input.inputs.width)) || !validDimension(Number(input.inputs.height)))) {
      ++draftRevision.current; setSaveState('尺寸待完善'); return
    }
    const revision = ++draftRevision.current
    setSaveState('保存中…')
    const timer = setTimeout(() => {
      api('/drafts/main', 'PUT', savedDraft(draft)).then(() => { if (draftRevision.current === revision) setSaveState('已保存到本地') }).catch(e => { if (draftRevision.current === revision) setSaveState('保存失败 · ' + e.message) })
    }, 650)
    return () => clearTimeout(timer)
  }, [draft])

  useEffect(() => {
    if (modal !== 'logs') return
    const load = () => api<{ time: number; message: string }[]>(`/accounts/${account}/logs`).then(setLogs).catch(e => setError(e.message))
    load(); const timer = setInterval(load, 3000); return () => clearInterval(timer)
  }, [modal, account])
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer) }, [notice])

  const profile = state?.accounts.find(p => p.id === account)
  const machine = profile?.machine
  const computing = ['checking', 'provisioning', 'deploying', 'stopping'].includes(machine?.phase ?? '')
  const allJobs = state?.jobs ?? []
  const jobs = allJobs.filter(j => !j.deleted_at)
  const running = jobs.filter(j => j.account === account && activeStatuses.has(j.status))
  const displayedJob = jobs.find(j => j.id === selectedJob)
  const video = displayedJob?.outputs.find(o => /\.(mp4|webm|mov)$/i.test(o.filename))
  const images = displayedJob?.outputs.filter(o => /\.(png|jpg|jpeg|webp)$/i.test(o.filename)) ?? []
  const inputNode = getNode(draft?.graph, 'MiniMaxH3ImageToVideo')
  const samplerNode = getNode(draft?.graph, 'BasicScheduler')
  const noiseNode = getNode(draft?.graph, 'RandomNoise')
  const createNode = getNode(draft?.graph, 'CreateVideo')
  const prompt = String(inputNode?.inputs.prompt ?? '')
  const frames = Number(inputNode?.inputs.length ?? 124)
  const fps = Number(createNode?.inputs.fps ?? 24)
  const nativeUrl = machine?.phase === 'ready' && machine.port ? `http://127.0.0.1:${machine.port}` : undefined

  async function action(name: string, fn: () => Promise<unknown>) {
    setBusy(name); setError('')
    try { await fn(); await refresh() } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  function updateNode(type: string, values: Record<string, unknown>) {
    if (!draft) return
    const graph = structuredClone(draft.graph)
    const node = getNode(graph, type)
    if (!node) return setError('当前工作流不包含这个节点。请使用节点视图或 JSON 编辑。')
    Object.assign(node.inputs, values); setDraft({ ...draft, graph })
  }
  function openJson() { setJson(JSON.stringify(draft?.graph, null, 2)); setModal('json') }
  function exportGraph() {
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(draft?.graph, null, 2)], { type: 'application/json' })); a.download = 'workflow-api.json'; a.click(); URL.revokeObjectURL(a.href)
  }
  async function importFile(file?: File) {
    if (!file || !draft) return
    await action('import', async () => {
      if (file.size > 2_000_000) throw new Error('工作流文件不能超过 2 MB')
      const graph = JSON.parse(await file.text())
      const next = { name: file.name.replace(/\.json$/i, ''), graph, positions: {} }
      await api('/drafts/main', 'PUT', savedDraft(next)); setDraft(next); selectJob(null); setView('workflow'); setPage('studio'); setNotice('工作流已导入')
    })
  }
  async function generate() {
    if (!draft) return
    await action('generate', async () => {
      if (inputNode && !prompt.trim()) { setMobilePanel('editor'); throw new Error('请先填写画面描述') }
      if (inputNode && (!validDimension(Number(inputNode.inputs.width)) || !validDimension(Number(inputNode.inputs.height)))) {
        setMobilePanel('editor'); throw new Error(resolutionError)
      }
      await api('/drafts/main', 'PUT', savedDraft(draft))
      const job = await api<Job>('/jobs', 'POST', { ...draft, name: draft.name.trim() || `视频 · ${when(Date.now() / 1000)}`, account, stop_after: stopAfter })
      selectJob(job.id); setNotice(machine?.phase === 'ready' ? '任务已加入队列' : '任务已创建，开始准备 GPU')
    })
  }
  async function reuse(job: Job) {
    await action('reuse', async () => { const full = await api<Job>(`/jobs/${job.id}`); if (full.graph) { if (draft && draft !== initialDraft.current) await api(`/drafts/${crypto.randomUUID()}`, 'PUT', savedDraft(draft)); setDraft({ name: job.name, graph: full.graph, positions: {} }); } setPage('studio'); setView('workflow'); selectJob(null); setNotice(draft === initialDraft.current ? '参数已载入' : '参数已载入，原草稿已保存副本') })
  }

  if (!state || !draft) return <div className="loading-screen"><h2>视频工作台</h2><p>{error || '正在连接本地工作台…'}</p>{error && <button className="primary" onClick={() => location.reload()}>重新连接</button>}</div>

  return <div className="app-shell">
    <aside className="rail">
      <div className="rail-nav"><button aria-label="创作工作台" title="创作工作台" className={page === 'studio' ? 'active' : ''} onClick={() => setPage('studio')}><SlidersHorizontal size={20} /><span>创作</span></button>
        <button aria-label="成片与任务" title="成片与任务" className={page === 'library' ? 'active' : ''} onClick={() => setPage('library')}><LayoutGrid size={20} /><span>作品</span>{running.length > 0 && <i />}</button></div>
      <div className="rail-bottom"><button aria-label="使用说明" title="使用说明" onClick={() => setModal('help')}><CircleHelp size={20} /></button><button aria-label="工作台设置" title="设置" aria-haspopup="dialog" className={modal === 'settings' ? 'active' : ''} onClick={() => setModal('settings')}><Settings2 size={20} /></button></div>
    </aside>
    <div className="workspace">
      <header className="workspace-header">
        <div className="workspace-heading"><h1>{page === 'studio' ? '创作工作台' : '作品管理'}</h1><span className="local-status"><span className={`tiny-dot ${connected ? '' : 'muted'}`} />{connected ? '本地服务在线' : '本地服务离线'}</span></div>
        <div className="header-actions">
          <div className="header-workflow-actions" hidden={page !== 'studio'}><button className="secondary" aria-label="导入工作流" title="导入工作流" onClick={() => fileInput.current?.click()}><Upload size={14} /><span className="action-label">导入工作流</span></button><button className="secondary" aria-label="导出" title="导出工作流" onClick={exportGraph}><Download size={14} /><span className="action-label">导出</span></button></div>
          <button className="text-button" aria-label="草稿" title="草稿" onClick={() => setModal('drafts')}><FolderOpen size={15} /><span className="action-label">草稿</span></button>
          <button className="icon-button" aria-label="更多工作流操作" title="更多工作流操作" onClick={openJson}><MoreHorizontal size={20} /></button>
        </div>
        <input hidden type="file" accept=".json,application/json" ref={fileInput} onChange={e => { importFile(e.target.files?.[0]); e.target.value = '' }} />
      </header>
      {error && <div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭错误" onClick={() => setError('')}><X size={15} /></button></div>}
      {notice && <div className="toast" role="status"><Check size={15} />{notice}</div>}

      {page === 'studio' && <><nav className="mobile-tabs" aria-label="工作区">{(['stage', 'editor', 'compute'] as const).map((p, i) => <button key={p} aria-pressed={mobilePanel === p} className={mobilePanel === p ? 'active' : ''} onClick={() => setMobilePanel(p)}>{['预览与生成', '创作参数', '算力与性能'][i]}</button>)}</nav><div className={`studio-grid mobile-${mobilePanel}`}>
        <section className="editor-panel panel"><header className="panel-heading"><span><SlidersHorizontal size={15} /> 创作参数</span><span className="subtle">01</span></header>
          <div className="editor-body"><div className="preset-card"><div className="preset-icon"><Film size={19} /></div><div><strong>MiniMax H3</strong><small>INT8 ConvRot · 本次 {Number(samplerNode?.inputs.steps ?? 20)} 步</small></div><span className="tag">预设</span></div>
            <label className="field"><span>项目名称</span><input aria-label="项目名称" value={draft.name} placeholder="作品名称（可选）" onChange={e => setDraft({ ...draft, name: e.target.value })} maxLength={100} /></label>
            <label className="field prompt-field"><span>画面描述 <span className="label-hint">PROMPT</span></span><textarea aria-label="画面描述" value={prompt} placeholder="描述主体、动作和镜头…" onChange={e => updateNode('MiniMaxH3ImageToVideo', { prompt: e.target.value })} spellCheck={false} disabled={!inputNode} /></label>
            <Resolution width={Number(inputNode?.inputs.width ?? 1344)} height={Number(inputNode?.inputs.height ?? 768)} disabled={!inputNode} change={values => updateNode('MiniMaxH3ImageToVideo', values)} />
            <div className="field-row"><label className="field"><span>帧数 <span className="label-hint">{(frames / fps).toFixed(2)}s</span></span><input aria-label="帧数" type="number" min={4} max={512} step={4} value={frames} onChange={e => updateNode('MiniMaxH3ImageToVideo', { length: Number(e.target.value) })} /></label><label className="field"><span>帧率</span><input aria-label="帧率" type="number" min={1} max={60} value={fps} onChange={e => updateNode('CreateVideo', { fps: Number(e.target.value) })} /></label></div>
            <div className="field-row sampling-fields"><label className="field"><span>采样步数</span><input aria-label="采样步数" type="number" min={1} max={100} value={Number(samplerNode?.inputs.steps ?? 20)} onChange={e => updateNode('BasicScheduler', { steps: Number(e.target.value) })} /></label><label className="field seed-field"><span>随机种子<button className="icon-button" aria-label="随机生成种子" title="随机生成种子" onClick={() => updateNode('RandomNoise', { noise_seed: Math.floor(Math.random() * 2 ** 32) })}><RefreshCw size={12} /></button></span><input aria-label="随机种子" type="number" min={0} max={Number.MAX_SAFE_INTEGER} value={Number(noiseNode?.inputs.noise_seed ?? 0)} onChange={e => updateNode('RandomNoise', { noise_seed: Number(e.target.value) })} /></label></div>
            <div className="config-footnote"><span>{String(getNode(draft.graph, 'KSamplerSelect')?.inputs.sampler_name ?? '自定义采样')}</span><span>{String(samplerNode?.inputs.scheduler ?? '自定义')}</span><span>{Object.values(draft.graph).some(n => /lora/i.test(n.class_type)) ? '包含 LoRA 节点' : '无 LoRA 节点'}</span></div>
          </div><footer className="editor-footer"><span className={saveState.startsWith('保存失败') ? 'danger' : ''}><Check size={12} />{saveState}</span><button className="text-button" onClick={() => action('snapshot', async () => { await api(`/drafts/${crypto.randomUUID()}`, 'PUT', savedDraft(draft)); setNotice('草稿副本已保存') })}><Save size={13} />另存</button></footer>
        </section>

        <section className="stage-column"><div className="stage panel"><header className="stage-heading"><div className="segmented"><button className={view === 'preview' ? 'active' : ''} onClick={() => setView('preview')}><Play size={13} /> 预览</button><button className={view === 'workflow' ? 'active' : ''} onClick={() => setView('workflow')}><Network size={13} /> 工作流</button></div><span className="subtle">{view === 'preview' ? '画面 · 声音' : `${Object.keys(draft.graph).length} 个节点`}</span></header>
          {view === 'workflow' ? <Workflow draft={draft} update={setDraft} openJson={openJson} nativeUrl={nativeUrl} fail={setError} /> : video || images.length ? <div className="preview-wrap"><div className="preview-top"><span className="preview-badge"><span className="tiny-dot" />本地成片</span><span className="preview-corner">H3 / INT8</span></div>
            {video ? <video key={video.url} controls preload="metadata" src={video.url} /> : <img alt="生成结果" src={images[0].url} />}
            <div className="preview-caption"><span>{displayedJob?.name}<small>{displayedJob?.account} · 已保存到本地</small></span><a className="preview-download" aria-label="下载当前成片" href={(video || images[0]).url} download><ArrowDownToLine size={17} /></a></div></div> : <div className="preview-empty" role="status">{displayedJob && activeStatuses.has(displayedJob.status) ? <><LoaderCircle size={28} className="spin" /><strong>{statusLabel[displayedJob.status]}</strong><span>{displayedJob.message}</span></> : <><Film size={32} strokeWidth={1.2} /><strong>{displayedJob ? statusLabel[displayedJob.status] : '等待创作'}</strong><span>{displayedJob ? displayedJob.message : '生成后在这里预览'}</span></>}</div>}
          <div className="stage-status"><span><span className={`tiny-dot ${machine?.phase === 'ready' ? '' : 'muted'}`} />{running.length ? running[0].message : '准备好下一次创作'}</span><button className="text-button" onClick={() => setPage('library')}>全部任务 <ArrowUpRight size={13} /></button></div>
        </div>
        <div className="render-bar panel"><div><div className="render-title"><Sparkles size={16} /><strong>{running.length ? `${running.length} 个任务进行中` : machine?.phase === 'ready' ? 'GPU 已就绪' : '按需启动 GPU'}</strong></div></div><button className="primary generate" onClick={generate} disabled={!!busy || !profile?.credential_present || machine?.phase === 'stopping'}><Play size={16} fill="currentColor" />{busy === 'generate' ? '提交中…' : running.length ? '加入队列' : '生成视频'}<span>↗</span></button></div>
        <div className="under-stage"><label className="checkbox"><input type="checkbox" checked={stopAfter} onChange={e => setStopAfter(e.target.checked)} />取回成片后关闭 GPU</label></div>
        {running.length > 0 && <div className="current-jobs">{running.slice(0, 1).map(j => <div className="current-job" key={j.id}><div><span className="status-chip working"><LoaderCircle size={12} className="spin" />{statusLabel[j.status]}</span><strong>{j.name}</strong><span>{j.message}</span></div><div className="inline-actions">{j.status === 'recovering' && <button className="text-button" disabled={!!busy} onClick={() => action('resume', () => api(`/jobs/${j.id}/resume`, 'POST'))}>恢复查询</button>}<button className="text-button" onClick={() => action('cancel', () => api(`/jobs/${j.id}/cancel`, 'POST'))} disabled={!!busy}>取消</button></div>{j.progress != null && <div className="progress-track"><span style={{ width: `${j.progress}%` }} /></div>}</div>)}</div>}
        </section>

        <aside className="compute-column"><section className="compute-panel panel"><header className="panel-heading"><span><Cloud size={16} /> 云端计算</span><button className="icon-button" aria-label="刷新账号状态" title="刷新余额与会话" disabled={!!busy} onClick={() => action('refresh', () => api(`/accounts/${account}/refresh`, 'POST'))}><RefreshCw size={14} className={busy === 'refresh' ? 'spin' : ''} /></button></header><div className="compute-body">
          <label className="field account-field"><span>当前账号</span><select aria-label="当前账号" value={account} onChange={e => { setAccount(e.target.value); localStorage.setItem('fieldwork-account', e.target.value) }}>{state.accounts.map(p => <option value={p.id} key={p.id}>{p.id} · {p.credential_present ? '已配置' : '待登录'}</option>)}</select></label>
          {state.accounts.filter(p => p.id !== account && p.machine.session).map(p => <button className="other-account" key={p.id} onClick={() => { setAccount(p.id); localStorage.setItem('fieldwork-account', p.id) }}><span className="tiny-dot" />{p.id} 的机器仍在运行<ArrowUpRight size={12} /></button>)}
          <div className="gpu-summary"><span><Cpu size={15} />RTX PRO 6000 <small>96 GB</small></span><span className={`status-chip ${machine?.phase === 'ready' ? 'success' : computing ? 'working' : ''}`}>{phaseLabel[machine?.phase ?? 'stopped']}</span></div>
          <div className="usage-grid"><div><span>账号余额</span><strong>{profile?.usage?.balance?.toLocaleString('en-US', { maximumFractionDigits: 2 }) ?? '—'}<small> CU</small></strong></div><div><span>账号消耗</span><strong>{profile?.usage?.rate?.toFixed(2) ?? '—'}<small> /h</small></strong></div></div>
          {(profile?.error || machine?.phase !== 'ready') && <div className="machine-message" title={profile?.error || machine?.message}>{profile?.error || machine?.message}</div>}{(profile?.sessions ?? []).filter(s => s.name !== machine?.session).length > 0 && <div className="external-sessions">另有 {(profile?.sessions ?? []).filter(s => s.name !== machine?.session).length} 台外部会话</div>}
          {machine?.phase === 'ready' ? <a className="secondary full" href={nativeUrl} target="_blank" rel="noreferrer"><ExternalLink size={14} />打开 ComfyUI</a> : <button className="secondary full" disabled={!!busy || computing || !profile?.credential_present} onClick={() => action('start', () => api(`/accounts/${account}/start`, 'POST'))}>{computing ? <LoaderCircle size={14} className="spin" /> : <Power size={14} />}{computing ? '正在准备…' : machine?.session ? '重新连接 / 重试部署' : '启动 GPU'}</button>}
          {machine?.session && <button className="stop-button" disabled={!!busy || running.length > 0 || machine.phase === 'stopping'} title={running.length ? '请先取消未完成任务' : '释放这个工作台的云端机器'} onClick={() => action('stop', () => api(`/accounts/${account}/stop`, 'POST'))}><Power size={13} />{machine.phase === 'stopping' ? '正在关闭…' : '关闭并释放机器'}</button>}
          <div className="compute-footer"><button className="text-button" onClick={() => setModal('logs')}><Terminal size={12} />部署日志</button><button className="text-button" aria-haspopup="dialog" onClick={() => setModal('settings')}>{state.settings.idle_minutes ? `空闲 ${state.settings.idle_minutes}m 关闭` : '手动关机'}</button></div>
        </div></section>
        {machine && <Performance account={account} machine={machine} />}
        </aside>
      </div></>}

      {page === 'library' && <Library jobs={allJobs} accounts={state.accounts.map(a => a.id)} reuse={reuse} create={() => setPage('studio')} refresh={refresh} request={api} notify={setNotice} />}
    </div>

    {modal === 'settings' && <SettingsDialog saved={state.settings} accounts={state.accounts} account={account} busy={!!busy} close={() => setModal(null)} chooseAccount={id => { setAccount(id); localStorage.setItem('fieldwork-account', id) }} save={async value => {
      const saved = await api<Settings>('/settings', 'PUT', value)
      setState(previous => previous ? { ...previous, settings: saved } : previous)
      setModal(null)
      setNotice('设置已保存')
    }} />}

    {modal && modal !== 'settings' && <Dialog title={modal === 'json' ? '编辑 API 工作流' : modal === 'logs' ? `${account} · 部署日志` : modal === 'drafts' ? '本地草稿' : '使用说明'} close={() => setModal(null)} wide={modal === 'logs' || modal === 'json'} busy={!!busy}>
      {modal === 'json' && <><p>ComfyUI 的 API 格式。可以修改节点、连接、模型与参数。</p><textarea className="json-editor" aria-label="API JSON" value={json} onChange={e => setJson(e.target.value)} spellCheck={false} /><footer><button className="secondary" onClick={exportGraph}><FileJson size={14} />导出当前工作流</button><button className="primary" onClick={() => action('json', async () => { const graph = JSON.parse(json); const next = { ...draft, graph }; await api('/drafts/main', 'PUT', savedDraft(next)); setDraft(next); setModal(null); setView('workflow'); setNotice('工作流已更新') })}>应用修改</button></footer></>}
      {modal === 'logs' && <><p>安装、下载、连接与机器生命周期记录。</p><div className="logs">{logs.length ? logs.map((l, i) => <div key={i}><time>{when(l.time)}</time><pre>{l.message}</pre></div>) : <div className="log-empty">尚无部署记录。启动 GPU 后，这里会显示进度。</div>}</div></>}
      {modal === 'drafts' && <div className="draft-list">{state.drafts.length ? state.drafts.map(d => <button key={d.id} onClick={() => action('draft', async () => { const next = await api<Draft>(`/drafts/${d.id}`); setDraft({ name: next.name, graph: next.graph, positions: next.positions }); selectJob(null); setModal(null); setPage('studio') })}><FileJson size={18} /><span><strong>{d.name}{d.id === 'main' ? ' · 最近自动保存' : ''}</strong><small>{when(d.updated_at)}</small></span><ArrowUpRight size={14} /></button>) : <p>编辑工作流后会自动保存。</p>}<button onClick={() => action('template', async () => { const next = await api<Draft>('/template'); setDraft(next); selectJob(null); setModal(null); setPage('studio') })}><Plus size={18} /><span>载入已验证的 H3 模板</span></button></div>}
      {modal === 'help' && <div className="help-content"><div><b>01</b><section><h3>本地编辑</h3><p>修改提示词与参数，或在节点画布连接、编辑工作流。草稿自动保存。导入时请选择 ComfyUI 导出的 API JSON。</p></section></div><div><b>02</b><section><h3>选择账号，按需计算</h3><p>点击「生成视频」后自动申请 G4，安装固定版本和四个模型，再执行任务。首次需下载约 40 GB 模型；后续生成复用同一台机器。</p></section></div><div><b>03</b><section><h3>成片留在本机</h3><p>生成完成后自动取回视频和工作流。可选择任务完成后关闭 GPU，或者等待空闲关机。切换账号不关闭原账号机器。</p></section></div><p className="settings-note">完整 ComfyUI 编辑器通过本地隧道打开。在原生编辑器直接提交的任务可正常运行，但需自行导出成片；工作台只自动归档从这里提交的任务。新增自定义节点和模型需要另外配置部署。</p></div>}
    </Dialog>}
  </div>
}
