import { useEffect, useState } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import type { LLMService } from './types'

const finished = new Set(['ready', 'completed', 'done', 'restored'])
const active = new Set(['running', 'installing', 'downloading', 'verifying', 'extracting'])
const gib = (value: number) => `${(value / 2 ** 30).toFixed(1)} GiB`
const duration = (value: number) => value >= 60 ? `${Math.floor(value / 60)} 分 ${Math.floor(value % 60)} 秒` : `${Math.floor(value)} 秒`

export default function ServiceProgress({ service }: { service: LLMService }) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer) }, [])
  const deployment = service.deployment
  const phase = deployment?.phase ?? service.phase
  const ready = service.phase === 'ready'
  const failed = service.phase === 'error'
  const allocated = ready || service.phase === 'deploying' || !!deployment
  const loading = ['loading', 'connecting', 'ready'].includes(phase)
  const environment = deployment?.stages?.environment
  const download = deployment?.stages?.download
  const environmentDone = ready || finished.has(environment?.status ?? '') || loading
  const downloadDone = ready || finished.has(download?.status ?? '') || loading
  const bytes = download?.bytes ?? 0
  const total = download?.total_bytes ?? 0
  const percentage = total > 0 ? Math.min(100, bytes / total * 100) : null
  const rows = [
    { name: '申请算力', done: allocated, running: !allocated, detail: allocated ? undefined : service.message },
    { name: '安装环境', done: environmentDone, running: active.has(environment?.status ?? ''), detail: environment?.message },
    { name: '下载权重', done: downloadDone, running: !!download && !downloadDone, detail: total > 0 ? `${gib(bytes)} / ${gib(total)}` : undefined },
    { name: '加载模型', done: ready || phase === 'connecting', running: phase === 'loading', detail: undefined },
    { name: '连接端点', done: ready, running: phase === 'connecting', detail: undefined },
  ]
  return <div className="service-progress" aria-label="服务加载进度">
    <header><strong>{ready ? '服务已就绪' : failed ? '加载中断' : '正在启动'}</strong>{deployment?.started_at && <time>{duration(Math.max(0, ((ready || failed) ? deployment.time ?? deployment.started_at : now) - deployment.started_at))}</time>}</header>
    <ol>{rows.map(row => <li key={row.name} className={row.done ? 'done' : row.running ? 'running' : 'waiting'}>
      <span className="service-progress-icon">{row.done ? <Check size={13} /> : row.running && !failed ? <LoaderCircle size={13} className="spin" /> : <i />}</span>
      <div><div className="service-progress-row"><span>{row.name}</span><small>{row.done ? '完成' : failed ? '已停止' : row.running ? '进行中' : '等待'}</small></div>
        {row.detail && <p>{row.detail}</p>}
        {row.name === '下载权重' && download && <>
          {percentage !== null && <progress aria-label="权重下载进度" value={bytes} max={total} />}
          <div className="service-transfer-stats">{percentage !== null && <span>{percentage.toFixed(1)}%</span>}{!!download.rate_bytes_s && <span>{(download.rate_bytes_s / 2 ** 20).toFixed(0)} MiB/s</span>}{download.files_total !== undefined && <span>{download.files_done ?? 0} / {download.files_total} 文件</span>}</div>
          {download.current && !downloadDone && <p className="service-transfer-file" title={download.current}>{download.current.split('/').pop()}</p>}
        </>}
      </div>
    </li>)}</ol>
    {service.phase === 'error' && <p className="service-progress-error">{service.message}</p>}
  </div>
}
