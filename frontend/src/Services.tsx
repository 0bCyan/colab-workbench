import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Check, ChevronRight, Cloud, Copy, Cpu, Film, KeyRound, LoaderCircle, Play, Power, RefreshCw, Server, Square, Terminal } from 'lucide-react'
import Dialog from './Dialog'
import Performance from './Performance'
import { type LLMConfig, type State, phaseLabel } from './types'
import './services.css'

type Props = {
  state: State
  account: string
  chooseAccount: (id: string) => void
  request: <T = unknown>(path: string, method?: string, body?: unknown) => Promise<T>
  refresh: () => Promise<unknown>
  notify: (message: string) => void
  openSettings: () => void
  openVideo: () => void
}

const defaults: LLMConfig = { preset_id: 'qwen27-fp8', context_tokens: 262144, gpu_memory: .9, cpu_threads: 24 }
const busyPhases = new Set(['checking', 'provisioning', 'deploying', 'stopping'])

export default function Services({ state, account, chooseAccount, request, refresh, notify, openSettings, openVideo }: Props) {
  const service = state.llm?.services.find(s => s.account === account)
  const presets = state.llm?.presets ?? []
  const profile = state.accounts.find(a => a.id === account)
  const [config, setConfig] = useState<LLMConfig>(service?.config ?? defaults)
  const [engine, setEngine] = useState('all')
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')
  const [tab, setTab] = useState<'test' | 'example'>('test')
  const [mobile, setMobile] = useState<'config' | 'endpoint' | 'compute'>('endpoint')
  const [prompt, setPrompt] = useState('用一句话介绍自己。')
  const [answer, setAnswer] = useState('')
  const [reasoning, setReasoning] = useState('')
  const [testing, setTesting] = useState(false)
  const [logs, setLogs] = useState<{ time: number; message: string }[] | null>(null)
  const controller = useRef<AbortController | null>(null)
  const serial = useRef(0)
  const currentAccount = useRef(account)
  currentAccount.current = account
  const output = useRef<HTMLDivElement>(null)
  const savedConfig = JSON.stringify(service?.config ?? defaults)
  useEffect(() => { setConfig(JSON.parse(savedConfig)); setError('') }, [account, savedConfig])
  useEffect(() => {
    ++serial.current
    controller.current?.abort()
    setAnswer(''); setReasoning(''); setTesting(false); setLogs(null); setPending(''); setCopied('')
    return () => { ++serial.current; controller.current?.abort() }
  }, [account])
  useEffect(() => { if (copied) { const timer = setTimeout(() => setCopied(''), 2000); return () => clearTimeout(timer) } }, [copied])
  useEffect(() => { output.current?.scrollTo({ top: output.current.scrollHeight }) }, [answer, reasoning])
  useEffect(() => {
    if (logs === null || !account) return
    let alive = true
    const load = () => request<{ time: number; message: string }[]>(`/llm/${account}/logs`).then(items => { if (alive) setLogs(items) }).catch(() => {})
    load(); const timer = setInterval(load, 3000)
    return () => { alive = false; clearInterval(timer) }
    // Keep the log polling active across log updates.
  }, [logs === null, account, request])

  const recipe = presets.find(p => p.id === config.preset_id)
  const activeRecipe = presets.find(p => p.id === service?.config.preset_id)
  const ready = service?.phase === 'ready'
  const working = busyPhases.has(service?.phase ?? '')
  const locked = !!service?.session || working || !!pending
  const available = !!profile?.installed && !!profile.credential_present
  const dirty = savedConfig !== JSON.stringify(config)
  const endpoint = service ? `${location.origin}${service.endpoint_path}` : ''
  const servedModel = service?.model_name ?? 'workbench-llm'
  const external = (profile?.sessions ?? []).filter(s => s.name !== service?.session && s.name !== profile?.machine.session)
  const curl = `curl '${endpoint}/chat/completions' \\\n  -H "Authorization: Bearer $WORKBENCH_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify({ model: servedModel, messages: [{ role: 'user', content: '你好' }], stream: true })}'`

  async function act(label: string, task: () => Promise<unknown>) {
    const target = account
    setPending(label); setError('')
    try { await task(); await refresh() } catch (e) { if (target === currentAccount.current) setError((e as Error).message) } finally { if (target === currentAccount.current) setPending('') }
  }
  async function copy(label: string, value: string) {
    try { await navigator.clipboard.writeText(value); setCopied(label); notify(`${label}已复制`) } catch { setError('无法访问剪贴板，请手动复制') }
  }
  async function copyKey() {
    await act('key', async () => { const result = await request<{ key: string }>('/llm/key', 'POST'); await copy('密钥', result.key) })
  }
  async function save() {
    await request(`/llm/${account}/config`, 'PUT', config)
  }
  async function test() {
    if (!prompt.trim() || !ready || testing) return
    const turn = ++serial.current
    const abort = new AbortController()
    controller.current = abort
    setTesting(true); setAnswer(''); setReasoning(''); setError('')
    try {
      const { key } = await request<{ key: string }>('/llm/key', 'POST')
      if (abort.signal.aborted) return
      const response = await fetch(`${endpoint}/chat/completions`, { method: 'POST', signal: abort.signal,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
        body: JSON.stringify({ model: servedModel, messages: [{ role: 'user', content: prompt }], max_tokens: 512, stream: true }) })
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.error?.message ?? data.detail ?? '测试请求失败') }
      if (!response.body) throw new Error('浏览器不支持流式响应')
      const reader = response.body.getReader(), decoder = new TextDecoder()
      let buffer = '', received = false
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const lines = buffer.split('\n'); buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data:') || line.slice(5).trim() === '[DONE]') continue
          const data = JSON.parse(line.slice(5))
          if (data.error) throw new Error(data.error.message ?? '生成中断')
          const delta = data.choices?.[0]?.delta
          if (serial.current !== turn) return
          if (typeof delta?.content === 'string') { received = true; setAnswer(previous => previous + delta.content) }
          if (typeof (delta?.reasoning_content ?? delta?.reasoning) === 'string') { received = true; setReasoning(previous => previous + (delta.reasoning_content ?? delta.reasoning)) }
        }
        if (done) break
      }
      if (!received) throw new Error('服务没有返回文本，请检查部署日志')
    } catch (e) { if (!abort.signal.aborted && serial.current === turn) setError((e as Error).message) }
    finally { if (serial.current === turn) { setTesting(false); controller.current = null; refresh().catch(() => {}) } }
  }

  return <>
    <nav className="mobile-tabs service-tabs" aria-label="服务工作区">{(['config', 'endpoint', 'compute'] as const).map((p, i) => <button key={p} className={mobile === p ? 'active' : ''} aria-pressed={mobile === p} onClick={() => setMobile(p)}>{['部署配置', '端点与测试', '算力与性能'][i]}</button>)}</nav>
    <div className={`services-grid service-mobile-${mobile}`}>
      <section className="panel service-config"><header className="panel-heading"><span><Server size={15} />服务配置</span><span className="subtle">LLM</span></header>
        <div className="service-config-body">
          <div className="service-kind"><button type="button" onClick={openVideo}><Film size={15} />视频生成<ArrowUpRight size={12} /></button><span><Server size={15} />语言模型<Check size={13} /></span></div>
          <label className="field"><span>部署引擎</span><select aria-label="部署引擎" disabled={locked || !service} value={engine} onChange={e => { const value = e.target.value; setEngine(value); const first = presets.find(p => p.engine === value); if (first && recipe?.engine !== value) setConfig({ ...config, preset_id: first.id }) }}><option value="all">全部引擎</option><option value="vllm">vLLM</option><option value="llama.cpp">llama.cpp</option></select></label>
          <div className="service-presets" role="group" aria-label="模型预设">{presets.filter(p => engine === 'all' || p.engine === engine).map(p => <button type="button" disabled={locked || !service} key={p.id} aria-pressed={config.preset_id === p.id} onClick={() => setConfig({ ...config, preset_id: p.id })}>
            <span><strong>{p.name}</strong><Check size={14} /></span><small>{p.engine === 'vllm' ? 'vLLM' : p.engine} · {p.quantization}<i>{p.reduced_refusal ? '少拒答' : p.tier}</i></small>
          </button>)}</div>
          {!presets.length && <p className="service-hint">请重启本地服务以加载模型预设</p>}
          <div className="service-options"><label className="field"><span>总上下文</span><select aria-label="总上下文" disabled={locked || !service} value={config.context_tokens} onChange={e => setConfig({ ...config, context_tokens: Number(e.target.value) })}><option value={32768}>32K</option><option value={131072}>128K</option><option value={262144}>256K</option></select></label>
            <label className="field"><span>{recipe?.engine === 'llama.cpp' ? 'CPU 线程' : '显存预算'}</span>{recipe?.engine === 'llama.cpp' ? <select aria-label="CPU 线程" disabled={locked || !service} value={config.cpu_threads} onChange={e => setConfig({ ...config, cpu_threads: Number(e.target.value) })}>{[8, 16, 24, 48].map(n => <option key={n} value={n}>{n}</option>)}</select> : <select aria-label="显存预算" disabled={locked || !service} value={config.gpu_memory} onChange={e => setConfig({ ...config, gpu_memory: Number(e.target.value) })}>{[.8, .9, .95].map(n => <option key={n} value={n}>{Math.round(n * 100)}%</option>)}</select>}</label></div>
          <p className="service-hint">单路 · 含输入与输出 · 预设待实机验收</p>
        </div>
        <footer className="service-config-footer"><span>{recipe ? `权重约 ${recipe.size_gib.toFixed(1)} GiB` : '未选择模型'}</span><button className="text-button" disabled={!dirty || locked || !service} onClick={() => act('save', async () => { await save(); notify('部署配置已保存') })}>{pending === 'save' ? '保存中…' : '保存配置'}<ChevronRight size={13} /></button></footer>
      </section>

      <section className="service-center">
        <div className="panel endpoint-panel"><header className="panel-heading"><span><Cloud size={15} />本地服务端点</span><span className={`status-chip ${ready ? 'success' : working ? 'working' : ''}`}>{phaseLabel[service?.phase ?? 'stopped']}</span></header>
          <div className="endpoint-body"><div className="service-title"><h2>{activeRecipe?.name ?? '选择一个账号'}</h2><span>{activeRecipe?.engine === 'vllm' ? 'vLLM' : activeRecipe?.engine} · OpenAI 兼容</span></div>
            <div className="endpoint-value"><code title={endpoint}>{endpoint || '未发现可用账号'}</code><button className="icon-button" disabled={!endpoint} title="复制端点" aria-label="复制端点" onClick={() => copy('端点', endpoint)}>{copied === '端点' ? <Check size={15} /> : <Copy size={15} />}</button></div>
            <div className="endpoint-details"><span>模型 <code>{servedModel}</code></span><button className="text-button" aria-label="复制服务密钥" disabled={!service || !!pending} onClick={copyKey}><KeyRound size={13} />{copied === '密钥' ? '密钥已复制' : '复制密钥'}</button></div>
            <p className="service-hint">端点固定绑定 {account || '账号'}，关闭后调用会返回未就绪。</p>
          </div></div>
        <div className="panel service-test"><header className="stage-heading"><div className="segmented"><button className={tab === 'test' ? 'active' : ''} onClick={() => setTab('test')}>连接测试</button><button className={tab === 'example' ? 'active' : ''} onClick={() => setTab('example')}>调用示例</button></div><span className="subtle">{testing ? '生成中' : '单路服务'}</span></header>
          {tab === 'example' ? <div className="service-example"><div><span>将密钥设置为 WORKBENCH_API_KEY</span><button className="icon-button" aria-label="复制调用示例" disabled={!endpoint} onClick={() => copy('调用示例', curl)}><Copy size={14} /></button></div><pre>{curl}</pre></div> : <>
            <div ref={output} className="service-output" aria-label="测试响应" aria-live="polite">{answer || reasoning ? <>{reasoning && <details className="service-reasoning"><summary>思考过程</summary><pre>{reasoning}</pre></details>}{answer && <pre>{answer}</pre>}{testing && <span className="service-cursor" />}</> : <div className="service-test-empty"><Server size={27} strokeWidth={1.2} /><strong>{ready ? '服务已就绪' : working ? service?.message : '启动后即可调用'}</strong><span>{ready ? '发送一条消息，检查端点连接' : '选择配置，按需启动云端服务'}</span></div>}</div>
            <form className="service-prompt" onSubmit={e => { e.preventDefault(); test() }}><textarea aria-label="测试消息" value={prompt} disabled={testing} onChange={e => setPrompt(e.target.value)} rows={2} maxLength={16000} /><div><small>{ready ? '测试使用当前运行的服务' : '服务就绪后可发送'}</small>{testing ? <button type="button" className="secondary" onClick={() => controller.current?.abort()}><Square size={13} />停止生成</button> : <button className="primary" disabled={!ready || !prompt.trim() || !!service?.active_requests}><Play size={13} />发送测试</button>}</div></form>
          </>}
          {error && <div className="service-error" role="alert">{error}<button aria-label="关闭服务错误" className="icon-button" onClick={() => setError('')}>×</button></div>}
        </div>
      </section>

      <aside className="compute-column service-compute"><section className="compute-panel panel"><header className="panel-heading"><span><Cloud size={15} />云端服务</span><button className="icon-button" aria-label="刷新服务账号" disabled={!available || !!pending} onClick={() => act('refresh', () => request(`/accounts/${account}/refresh`, 'POST'))}><RefreshCw size={14} className={pending === 'refresh' ? 'spin' : ''} /></button></header><div className="compute-body">
        <label className="field account-field"><span>服务账号</span><select aria-label="服务账号" value={account} disabled={!state.accounts.length || !!pending} onChange={e => chooseAccount(e.target.value)}>{!state.accounts.length && <option value="">未发现账号</option>}{state.accounts.map(a => <option key={a.id} value={a.id}>{a.id} · {state.llm?.services.find(s => s.account === a.id)?.phase === 'ready' ? '服务运行中' : !a.installed ? '未安装入口' : a.credential_present ? '已配置' : '待登录'}</option>)}</select></label>
        <div className="gpu-summary"><span><Cpu size={14} />RTX PRO 6000<small>96 GB</small></span><span className="tag">G4 普通</span></div>
        <div className="usage-grid"><div><span>账号余额</span><strong>{profile?.usage?.balance?.toLocaleString('en-US', { maximumFractionDigits: 2 }) ?? '—'}<small> CU</small></strong></div><div><span>账号消耗</span><strong>{profile?.usage?.rate?.toFixed(2) ?? '—'}<small> /h</small></strong></div></div>
        <div className="service-machine-message" title={service?.message}>{service?.message ?? '请先配置本机账号'}</div>
        {external.length > 0 && !service?.session && <p className="service-external">此账号另有 {external.length} 台外部会话</p>}
        {!!profile?.machine.session && !service?.session && <p className="service-external">此账号有视频机器运行中</p>}
        <button className="primary full" disabled={!available || !service || !!pending || working || ready || external.length > 0 && !service.session || !!profile?.machine.session && !service.session} onClick={() => act('start', async () => { if (dirty) await save(); await request(`/llm/${account}/start`, 'POST') })}>{working ? <LoaderCircle size={14} className="spin" /> : <Power size={14} />}{working ? '正在准备…' : ready ? '服务已启动' : service?.session ? '重新连接 / 重试' : '启动服务'}</button>
        {(service?.session || working) && <button className="stop-button" disabled={!!pending || !!service?.active_requests || testing || service?.phase === 'stopping'} onClick={() => act('stop', () => request(`/llm/${account}/stop`, 'POST'))}><Power size={13} />关闭服务并释放机器</button>}
        <div className="compute-footer"><button className="text-button" disabled={!service} onClick={() => setLogs([])}><Terminal size={12} />服务日志</button><button className="text-button" onClick={openSettings}>{state.settings.idle_minutes ? `空闲 ${state.settings.idle_minutes}m 关闭` : '手动关机'}</button></div>
      </div></section>{service && <Performance account={account} machine={service} metricsPath={`/api/llm/${account}/metrics`} />}</aside>
    </div>
    {logs !== null && <Dialog title={`${account} · 服务日志`} close={() => setLogs(null)} wide><div className="logs">{logs.length ? logs.map((l, i) => <div key={i}><time>{new Date(l.time * 1000).toLocaleTimeString()}</time><pre>{l.message}</pre></div>) : <div className="log-empty">启动服务后显示部署进度。</div>}</div></Dialog>}
  </>
}
