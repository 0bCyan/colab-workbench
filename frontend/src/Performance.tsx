import { useEffect, useState } from 'react'
import { Activity, ArrowDown, ArrowUp, Cpu, Gauge, HardDrive, MemoryStick, Thermometer, Zap } from 'lucide-react'
import type { Machine, MetricSample, Metrics } from './types'

const value = (n: number | null | undefined, unit = '%', digits = 0) => n == null || !Number.isFinite(n) ? '—' : `${n.toFixed(digits)}${unit}`
const gib = (n: number | null | undefined) => value(n == null ? n : n / 2 ** 30, '', 1)
const bandwidth = (n: number | null | undefined) => n == null ? '—' : n >= 1024 ** 2 ? value(n / 1024 ** 2, ' MB/s', 1) : value(n / 1024, ' KB/s', 1)

function Sparkline({ samples, color }: { samples: (number | null | undefined)[]; color: string }) {
  const points = samples.map((n, i) => n == null || !Number.isFinite(n) ? null : `${i * 200 / 59},${27 - Math.max(0, Math.min(100, n)) * .24}`)
  const segments: string[][] = [[]]
  for (const point of points) { if (point === null) segments.push([]); else segments[segments.length - 1].push(point) }
  return <svg className="sparkline" viewBox="0 0 200 30" preserveAspectRatio="none" aria-hidden="true"><path d="M0 28H200" stroke="currentColor" opacity=".1" />{segments.filter(s => s.length).map((s, i) => <polyline key={i} points={s.join(' ')} fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" />)}{points.filter(Boolean).length === 1 && <circle cx="0" cy={points.find(Boolean)!.split(',')[1]} r="2" fill={color} />}</svg>
}

function Meter({ label, percent, detail, history, color, icon }: { label: string; percent: number | null | undefined; detail?: string; history: (number | null | undefined)[]; color: string; icon: React.ReactNode }) {
  return <div className="metric-card" aria-label={`${label}占用率`}><div className="metric-heading"><span>{icon}{label}</span><strong>{value(percent)}</strong></div><div className="metric-bar"><span style={{ width: `${percent == null ? 0 : Math.max(0, Math.min(100, percent))}%`, background: color }} /></div><div className="metric-bottom"><small title={detail}>{detail || '—'}</small><Sparkline samples={history} color={color} /></div></div>
}

export default function Performance({ account, machine }: { account: string; machine: Machine }) {
  const [result, setResult] = useState<{ metrics: Metrics; received: number } | null>(null)
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    setResult(null)
    const poll = async () => {
      try {
        const response = await fetch(`/api/accounts/${account}/metrics`, { signal: controller.signal })
        if (!response.ok) throw new Error('Metrics unavailable')
        const metrics: Metrics = await response.json()
        if (active) setResult({ metrics, received: Date.now() })
      } catch { if (active) setResult(null) }
      if (active) timer = setTimeout(poll, 2000)
    }
    poll()
    return () => { active = false; controller.abort(); clearTimeout(timer) }
  }, [account, machine.session, machine.phase])
  useEffect(() => { const timer = setInterval(() => setClock(Date.now()), 2000); return () => clearInterval(timer) }, [])
  const metrics = result?.metrics
  const live = metrics?.account === account && metrics.session === machine.session && metrics.status === 'live' && clock - (result?.received ?? 0) < 8000 && machine.phase !== 'stopped'
  const s: MetricSample | null = live ? metrics.sample : null
  const history = live ? metrics.history : []
  const offline = !machine.session || machine.phase === 'stopped'
  const status = offline ? 'GPU 未启动' : live ? '实时 · 2s' : metrics?.status === 'waiting' ? '等待采集' : '连接中断'
  return <section className={`performance-panel ${live ? 'live' : ''}`} aria-label="性能看板"><header><span><Activity size={14} />性能看板</span><span className="metric-status"><i />{status}</span></header>
    <div className="performance-meters">
      <Meter label="GPU" icon={<Cpu size={13} />} percent={s?.gpu?.percent} color="#88a65f" detail={s?.gpu?.name.replace('NVIDIA ', '').replace(' Blackwell Server Edition', '') || 'RTX PRO 6000'} history={history.map(h => h.gpu?.percent)} />
      <Meter label="显存" icon={<MemoryStick size={13} />} percent={s?.gpu?.memory_percent} color="#789a70" detail={s?.gpu ? `${gib(s.gpu.memory_used)} / ${gib(s.gpu.memory_total)} GiB` : '— / — GiB'} history={history.map(h => h.gpu?.memory_percent)} />
      <Meter label="CPU" icon={<Gauge size={13} />} percent={s?.cpu.percent} color="#9aa66b" detail={s?.cpu ? `${s.cpu.cores} vCPU` : '— vCPU'} history={history.map(h => h.cpu.percent)} />
      <Meter label="内存" icon={<MemoryStick size={13} />} percent={s?.memory.percent} color="#b1ae73" detail={s ? `${gib(s.memory.used)} / ${gib(s.memory.total)} GiB` : '— / — GiB'} history={history.map(h => h.memory.percent)} />
      <Meter label="磁盘" icon={<HardDrive size={13} />} percent={s?.disk.percent} color="#7b9671" detail={s ? `${gib(s.disk.used)} / ${gib(s.disk.total)} GiB` : '— / — GiB'} history={history.map(h => h.disk.percent)} />
    </div>
    <div className="sensor-row"><span><Thermometer size={13} />{value(s?.gpu?.temperature, ' °C')}</span><span title={s?.gpu?.power_limit ? `功率上限 ${s.gpu.power_limit} W` : '功率'}><Zap size={13} />{value(s?.gpu?.power, ' W')}</span></div>
    <div className="network-row"><span title="下载速率"><ArrowDown size={12} />{bandwidth(s?.network.rx_bytes_s)}</span><span title="上传速率"><ArrowUp size={12} />{bandwidth(s?.network.tx_bytes_s)}</span></div>
    <footer><span>COLAB · {account.toUpperCase()}</span><span>{s ? new Date(s.sampled_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}</span></footer>
  </section>
}
