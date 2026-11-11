import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowLeft, ArrowRight, Check, Clapperboard, FileJson, Film, Pencil, Play, RefreshCw, Search, Trash2, Undo2 } from 'lucide-react'
import Dialog from './Dialog'
import { activeStatuses, statusLabel, type Job } from './types'

type Operation = 'trash' | 'restore' | 'delete'
type Result = { ids: string[]; failed: { id: string; reason: string }[] }
type Props = { jobs: Job[]; accounts: string[]; reuse: (job: Job) => void; create: () => void; refresh: () => Promise<unknown>; request: <T>(path: string, method?: string, body?: unknown) => Promise<T>; notify: (message: string) => void }
const when = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const size = (bytes: number) => bytes >= 2 ** 30 ? `${(bytes / 2 ** 30).toFixed(1)} GB` : `${(bytes / 2 ** 20).toFixed(1)} MB`
const media = (job: Job) => job.outputs.find(output => /\.(mp4|webm|mov)$/i.test(output.filename))

export default function Library({ jobs, accounts, reuse, create, refresh, request, notify }: Props) {
  const [trash, setTrash] = useState(false)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [account, setAccount] = useState('all')
  const [selected, setSelected] = useState<string[]>([])
  const [page, setPage] = useState(0)
  const [capacity, setCapacity] = useState({ columns: 3, rows: 1 })
  const [confirm, setConfirm] = useState<{ action: Operation; ids: string[] } | null>(null)
  const [renaming, setRenaming] = useState<Job | null>(null)
  const [name, setName] = useState('')
  const [preview, setPreview] = useState<Job | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const grid = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!grid.current) return
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setCapacity({ columns: Math.max(1, Math.min(3, Math.floor((width + 14) / 314))), rows: Math.max(1, Math.min(3, Math.floor((height + 14) / 290))) })
    })
    observer.observe(grid.current)
    return () => observer.disconnect()
  }, [])
  const filtered = jobs.filter(job => !!job.deleted_at === trash && (account === 'all' || job.account === account) && (status === 'all' || (status === 'active' ? activeStatuses.has(job.status) : job.status === status)) && job.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const pageSize = capacity.columns * capacity.rows
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const current = Math.min(page, pages - 1)
  const visible = filtered.slice(current * pageSize, (current + 1) * pageSize)
  const eligible = visible.filter(job => !activeStatuses.has(job.status))
  const selectedIds = selected.filter(id => filtered.some(job => job.id === id && !activeStatuses.has(job.status)))
  const allChecked = eligible.length > 0 && eligible.every(job => selectedIds.includes(job.id))
  const trashCount = jobs.filter(job => job.deleted_at).length
  function reset() { setPage(0); setSelected([]); setError('') }
  function ask(action: Operation, ids: string[]) { setError(''); setConfirm({ action, ids }) }
  async function manage(action: Operation, ids: string[]) {
    setBusy(true); setError('')
    try {
      const result = await request<Result>('/artworks/manage', 'POST', { ids, action })
      await refresh()
      setSelected(previous => previous.filter(id => !result.ids.includes(id))); setConfirm(null)
      if (result.failed.length) setError(`${result.failed.length} 项未删除：${result.failed[0].reason}`)
      else notify(action === 'trash' ? `${result.ids.length} 项已移入回收站，可随时恢复` : action === 'restore' ? `已恢复 ${result.ids.length} 项作品` : `已永久删除 ${result.ids.length} 项`)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function rename() {
    if (!renaming) return
    setBusy(true); setError('')
    try { await request(`/jobs/${renaming.id}`, 'PATCH', { name: name.trim() }); await refresh(); setRenaming(null); notify('作品已重命名') }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function cancel(job: Job) {
    setBusy(true); setError('')
    try { await request(`/jobs/${job.id}/cancel`, 'POST'); await refresh(); notify('任务已取消') }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const confirmJobs = jobs.filter(job => confirm?.ids.includes(job.id))
  return <section className="library panel" aria-label="作品管理">
    <header className="library-toolbar"><div className="library-tabs"><button className={!trash ? 'active' : ''} onClick={() => { setTrash(false); reset() }}><Clapperboard size={15} />全部作品 <span className="count">{jobs.length - trashCount}</span></button><button className={trash ? 'active' : ''} onClick={() => { setTrash(true); setStatus('all'); reset() }}><Trash2 size={14} />回收站 <span className="count">{trashCount}</span></button></div>
      <label className="search-box"><Search size={14} /><input aria-label="搜索作品" placeholder="搜索作品" value={search} onChange={e => { setSearch(e.target.value); reset() }} /></label>
      <select aria-label="筛选账号" value={account} onChange={e => { setAccount(e.target.value); reset() }}><option value="all">全部账号</option>{accounts.map(id => <option key={id} value={id}>{id}</option>)}</select>
      <select aria-label="筛选状态" value={status} onChange={e => { setStatus(e.target.value); reset() }}><option value="all">全部状态</option><option value="completed">已完成</option><option value="active">进行中</option><option value="failed">未完成</option><option value="cancelled">已取消</option></select>
    </header>
    <div className="selection-bar"><label className="checkbox"><input type="checkbox" aria-label="选择本页作品" checked={allChecked} disabled={!eligible.length || busy} onChange={() => setSelected(allChecked ? selectedIds.filter(id => !eligible.some(job => job.id === id)) : [...new Set([...selectedIds, ...eligible.map(job => job.id)])])} />{selectedIds.length ? `已选 ${selectedIds.length} 项` : '选择本页'}</label>
      <div>{selectedIds.length ? <>{trash && <button className="text-button" disabled={busy} onClick={() => manage('restore', selectedIds)}><Undo2 size={14} />恢复所选</button>}<button className="text-button danger" disabled={busy} onClick={() => ask(trash ? 'delete' : 'trash', selectedIds)}><Trash2 size={14} />{trash ? '永久删除所选' : '删除所选'}</button><button className="text-button" onClick={() => setSelected([])}>取消选择</button></> : <span className="subtle">{trash ? '文件保留至永久删除' : `${filtered.length} 项 · 保存于本机`}</span>}</div>
    </div>
    {error && !confirm && !renaming && <p role="alert" className="library-error">{error}</p>}
    <div className="gallery-viewport" ref={grid}>
      {filtered.length ? <div className="job-grid" style={{ gridTemplateColumns: `repeat(${capacity.columns}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${capacity.rows}, minmax(0, 1fr))` }}>{visible.map(job => {
        const output = media(job), image = job.outputs.find(o => /\.(png|jpe?g|webp)$/i.test(o.filename)), active = activeStatuses.has(job.status)
        return <article className={`job-card ${selectedIds.includes(job.id) ? 'selected' : ''}`} key={job.id} aria-label={job.name}>
          <div className="job-thumbnail"><button className="open-artwork" aria-label={`预览 ${job.name}`} onClick={() => { setPreview(job); setError('') }}>{output ? <video preload="metadata" src={`${output.url}#t=0.1`} muted /> : image ? <img alt={job.name} src={image.url} /> : <Film size={30} strokeWidth={1} />}{output && <span className="play-overlay"><Play size={20} fill="currentColor" /></span>}</button><span className="job-account">{job.account}</span>{!active && <label className="job-select"><input type="checkbox" aria-label={`选择 ${job.name}`} checked={selectedIds.includes(job.id)} disabled={busy} onChange={e => setSelected(e.target.checked ? [...selectedIds, job.id] : selectedIds.filter(id => id !== job.id))} /></label>}</div>
          <div className="job-body"><div className="job-top"><h3 title={job.name}>{job.name}</h3><span className={`status-chip ${job.status === 'completed' ? 'success' : job.status === 'failed' ? 'failure' : active ? 'working' : ''}`}>{statusLabel[job.status]}</span></div><p className="job-message" title={job.message}>{job.message}</p><div className="job-meta"><span>{when(job.created_at)}</span><span>{size(job.outputs.reduce((n, o) => n + o.bytes, 0))}{job.ended_at && job.started_at ? ` · ${Math.round(job.ended_at - job.started_at)}s` : ''}</span></div>
            <div className="job-actions">{trash ? <><button className="text-button" disabled={busy} onClick={() => manage('restore', [job.id])}><Undo2 size={14} />恢复</button><button className="text-button danger" disabled={busy} onClick={() => ask('delete', [job.id])}><Trash2 size={14} />永久删除</button></> : <><button className="text-button" disabled={busy} onClick={() => reuse(job)}><RefreshCw size={13} />复用</button><div className="card-tools"><button className="icon-button" aria-label={`重命名 ${job.name}`} title="重命名" disabled={busy} onClick={() => { setRenaming(job); setName(job.name); setError('') }}><Pencil size={14} /></button>{job.outputs.length > 0 && <a className="icon-button" aria-label={`下载 ${job.name}`} title="下载作品" href={(output || job.outputs[0]).url} download><ArrowDownToLine size={15} /></a>}{active ? <button className="text-button danger" disabled={busy} onClick={() => cancel(job)}>取消任务</button> : <button className="icon-button danger" aria-label={`删除 ${job.name}`} title="移入回收站" disabled={busy} onClick={() => ask('trash', [job.id])}><Trash2 size={14} /></button>}</div></>}</div>
          </div>
        </article>
      })}</div> : <div className="empty"><Film size={32} strokeWidth={1} /><h2>{search || status !== 'all' || account !== 'all' ? '没有匹配的作品' : trash ? '回收站为空' : '还没有作品'}</h2>{search || status !== 'all' || account !== 'all' ? <button className="secondary" onClick={() => { setSearch(''); setStatus('all'); setAccount('all'); reset() }}>清除筛选</button> : !trash && <button className="secondary" onClick={create}>开始创作</button>}</div>}
    </div>
    <footer className="pagination"><span>{filtered.length ? `${current * pageSize + 1}–${Math.min((current + 1) * pageSize, filtered.length)} / ${filtered.length}` : '0 项'}</span><div><button className="icon-button" aria-label="上一页" disabled={current === 0} onClick={() => { setPage(current - 1); setSelected([]) }}><ArrowLeft size={15} /></button><span>{current + 1} / {pages}</span><button className="icon-button" aria-label="下一页" disabled={current + 1 === pages} onClick={() => { setPage(current + 1); setSelected([]) }}><ArrowRight size={15} /></button></div></footer>
    {confirm && <Dialog title={confirm.action === 'delete' ? '永久删除作品？' : '删除作品？'} close={() => { setConfirm(null); setError('') }} busy={busy}><p>{confirm.action === 'delete' ? '将删除本地成片、工作流和任务记录，无法恢复。' : '作品将移入回收站，成片和工作流会保留，可随时恢复。'}</p><div className="confirm-items">{confirmJobs.map(job => <div key={job.id}><Film size={14} /><span>{job.name}</span><small>{job.account}</small></div>)}</div><p className="confirm-total">共 {confirmJobs.length} 项 · {size(confirmJobs.reduce((n, job) => n + job.outputs.reduce((s, o) => s + o.bytes, 0), 0))}</p>{error && <p className="library-error" role="alert">{error}</p>}<footer><button className="secondary" autoFocus disabled={busy} onClick={() => { setConfirm(null); setError('') }}>取消</button><button className={confirm.action === 'delete' ? 'danger-button' : 'primary'} disabled={busy} onClick={() => manage(confirm.action, confirm.ids)}><Trash2 size={14} />{busy ? '处理中…' : confirm.action === 'delete' ? '永久删除' : '移入回收站'}</button></footer></Dialog>}
    {renaming && <Dialog title="重命名作品" close={() => { setRenaming(null); setError('') }} busy={busy}><form onSubmit={e => { e.preventDefault(); rename() }}><label className="field"><span>作品名称</span><input autoFocus aria-label="作品名称" value={name} onChange={e => setName(e.target.value)} maxLength={100} /></label>{error && <p role="alert" className="library-error">{error}</p>}<footer><button type="button" className="secondary" disabled={busy} onClick={() => { setRenaming(null); setError('') }}>取消</button><button className="primary" disabled={busy || !name.trim()}><Check size={14} />保存名称</button></footer></form></Dialog>}
    {preview && <Dialog title={preview.name} close={() => setPreview(null)} wide><div className="artwork-preview">{media(preview) ? <video src={media(preview)!.url} controls autoPlay /> : preview.outputs.find(o => /\.(png|jpe?g|webp)$/i.test(o.filename)) ? <img alt={preview.name} src={preview.outputs.find(o => /\.(png|jpe?g|webp)$/i.test(o.filename))!.url} /> : <p>{preview.message}</p>}</div><footer>{preview.outputs.length > 0 ? <a className="text-button" href={`/api/jobs/${preview.id}/files/workflow-api.json`} download><FileJson size={14} />下载工作流</a> : <span />}{!preview.deleted_at && <button className="primary" onClick={() => { reuse(preview); setPreview(null) }}><RefreshCw size={14} />复用参数</button>}</footer></Dialog>}
  </section>
}
