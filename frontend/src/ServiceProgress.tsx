import { useEffect, useState } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import type { Machine, Job } from './types'
import './progress.css'

const finished = new Set(['ready', 'completed', 'done', 'restored'])
const active = new Set(['running', 'installing', 'downloading', 'verifying', 'extracting'])
const gib = (value: number) => `${(value / 2 ** 30).toFixed(1)} GiB`
const duration = (value: number) => value >= 60 ? `${Math.floor(value / 60)} 分 ${Math.floor(value % 60)} 秒` : `${Math.floor(value)} 秒`

export default function ServiceProgress({ service, kind = 'llm', job }: { service: Machine; kind?: 'llm' | 'media'; job?: Job }) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer) }, [])
  const deployment = service.deployment
  const media = kind === 'media'
  const phase = deployment?.phase ?? service.phase
  const ready = service.phase === 'ready'
  const failed = service.phase === 'error' || phase === 'error' || job?.status === 'failed'
  const allocated = ready || service.phase === 'deploying' || !!deployment
  const loading = ['loading', 'starting', 'connecting', 'ready'].includes(phase)
  const environment = deployment?.stages?.environment
  const download = deployment?.stages?.download
  const environmentDone = ready || finished.has(environment?.status ?? '') || loading
  const downloadDone = ready || finished.has(download?.status ?? '') || loading
  const bytes = Math.max(0, download?.bytes ?? 0)
  const total = download?.total_bytes ?? 0
  const percentage = total > 0 ? Math.min(100, bytes / total * 100) : null
  const rows = [
    { name: '申请算力', done: allocated, running: !allocated, detail: allocated ? undefined : service.message },
    { name: '安装环境', done: environmentDone, running: active.has(environment?.status ?? ''), detail: environment?.message },
    { name: '下载权重', done: downloadDone, running: !!download && !downloadDone, detail: total > 0 ? `${gib(bytes)} / ${gib(total)}` : undefined },
    { name: media ? '启动 ComfyUI' : '加载模型', done: ready || phase === 'connecting', running: ['loading', 'starting'].includes(phase), detail: undefined },
    { name: '连接端点', done: ready, running: phase === 'connecting', detail: undefined },
  ]
  if (media && job) rows.push({ name: '加载模型与生成', done: job.status === 'completed',
    running: ['submitting', 'queued', 'running', 'downloading'].includes(job.status), detail: ready ? job.message : undefined })
  const started = deployment?.started_at ?? service.loading_started_at
  return <div className="service-progress" aria-label={media ? '创作加载进度' : '服务加载进度'}>
    <header><strong>{failed ? '加载中断' : media && ready && job ? '正在生成' : ready ? '服务已就绪' : '正在启动'}</strong>{started && <time>{duration(Math.max(0, ((failed || (ready && !job)) ? deployment?.time ?? started : now) - started))}</time>}</header>
    <ol>{rows.map(row => <li key={row.name} className={row.done ? 'done' : row.running && !failed ? 'running' : 'waiting'}>
      <span className="service-progress-icon">{row.done ? <Check size={13} /> : row.running && !failed ? <LoaderCircle size={13} className="spin" /> : <i />}</span>
      <div><div className="service-progress-row"><span>{row.name}</span><small>{row.done ? '完成' : failed ? '已停止' : row.running ? '进行中' : '等待'}</small></div>
        {row.detail && <p>{row.detail}</p>}
        {row.name === '下载权重' && download && <>
          {percentage !== null && <progress aria-label="权重下载进度" value={Math.min(bytes, total)} max={total} />}
          <div className="service-transfer-stats">{percentage !== null && <span>{percentage.toFixed(1)}%</span>}{!!download.rate_bytes_s && <span>{(download.rate_bytes_s / 2 ** 20).toFixed(0)} MiB/s</span>}{download.files_total !== undefined && <span>{download.files_done ?? 0} / {download.files_total} 文件</span>}</div>
          {download.current && !downloadDone && <p className="service-transfer-file" title={download.current}>{download.current.split('/').pop()}</p>}
        </>}
      {row.name === '加载模型与生成' && job?.progress != null && <><progress aria-label="生成进度" value={job.progress} max={100} /><div className="service-transfer-stats">{Math.round(job.progress)}%</div></>}
      </div>
    </li>)}</ol>
    {failed && <p className="service-progress-error">{job?.status === 'failed' ? job.message : service.message}</p>}
  </div>
}
