import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Background, Controls, Handle, Panel, Position, ReactFlow, applyEdgeChanges, applyNodeChanges,
  type Connection, type Node, type NodeProps, type ReactFlowInstance } from '@xyflow/react'
import { Check, Copy, Cpu, Film, Image as ImageIcon, Layers2, LoaderCircle, MoreHorizontal, Pause, Pencil,
  Play, Plus, Power, RotateCcw, Settings2, Square, Trash2, Type, X } from 'lucide-react'
import Dialog from '../Dialog'
import type { Account } from '../types'
import { STORAGE, cancelTask, deleteBoard, duplicateBoard, enqueue, initialDemo, loadDemo, makeNode, moveTask, names, patchBoard, patchNode,
  progress, tick, uid, unfinished, type CardData, type CreativeNode, type Demo, type Instance, type Kind, type Task } from './model'
import '@xyflow/react/dist/style.css'
import './canvas.css'

type CardView = CardData & { ui: { task?: Task; percent: number; stage: string; queue: number; route: string;
  run: () => void; edit: () => void; cancel: () => void } }
const icons = { image: ImageIcon, video: Film, text: Type }
const modelLabel = (kind: Kind) => kind === 'image' ? 'H3 Image · BF16' : kind === 'video' ? 'H3 Video · INT8' : '提示词'
const taskStage = (task: Task | undefined, now: number) => !task ? '未运行' : task.status === 'done' ? '已完成'
  : task.status === 'cancelled' ? '已取消' : task.status === 'queued' ? '排队中'
    : task.loading && now - task.startedAt! < 1600 ? `加载${names[task.kind]}模型` : `生成${names[task.kind]}`

function Scene({ id, seed, video }: { id: string; seed: number; video: boolean }) {
  const tone = seed % 3
  return <svg className={`cd-scene tone-${tone}`} viewBox="0 0 280 150" role="img" aria-label="演示结果示意图">
    <defs><linearGradient id={`sky-${id}`} x2="0" y2="1"><stop stopColor={tone === 0 ? '#e5e5c8' : '#e1e9de'} /><stop offset="1" stopColor="#bbcdb4" /></linearGradient></defs>
    <rect width="280" height="150" fill={`url(#sky-${id})`} /><circle cx={tone === 1 ? 65 : 205} cy="41" r="17" fill="#f6f5dc" />
    <path d="M0 91 55 45 106 90 156 54 230 103 280 75V150H0Z" fill="#a2b99b" />
    <path d="M0 117 54 84 116 119 182 72 235 110 280 94V150H0Z" fill="#74947b" />
    <path d="M0 126Q60 107 125 133T280 117V150H0Z" fill="#476f5b" />
    <path d="M130 121Q159 130 146 150H204Q174 130 153 119Z" fill="#dae4c9" opacity=".7" />
    {video && <g className="cd-ripples" stroke="#eff2d8" opacity=".5" fill="none"><path d="M17 138h67m112 4h48m-164 4h34" /></g>}
  </svg>
}

function CreativeCard({ id, data, selected }: NodeProps<Node<CardView, 'creative'>>) {
  const Icon = icons[data.kind], task = data.ui.task, busy = !!task && unfinished(task)
  const version = data.versions.find(v => v.id === data.activeVersionId) ?? data.versions.at(-1)
  return <article className={`cd-node ${selected ? 'is-selected' : ''} ${busy ? 'is-busy' : ''}`} aria-label={`节点 ${data.label}`}>
    <Handle type="target" position={Position.Left} isConnectable={data.kind !== 'text'} />
    <header><span className={`cd-kind ${data.kind}`}><Icon size={14} /></span><strong>{data.label}</strong>
      <button className="icon-button nodrag" aria-label={`设置 ${data.label}`} onClick={data.ui.edit}><MoreHorizontal size={16} /></button></header>
    {data.kind !== 'text' && <div className={`cd-node-preview ${version ? 'has-result' : ''}`}>
      {version ? <><Scene id={id} seed={version.seed} video={data.kind === 'video'} /><small>示意预览</small></>
        : <><Icon size={28} strokeWidth={1.2} /><span>{data.kind === 'image' ? '等待一幅新画面' : '等待一段新故事'}</span></>}
      {busy && <div className="cd-node-state"><LoaderCircle size={12} className="spin" />{task.status === 'queued' ? `排队 ${data.ui.queue}` : data.ui.stage}</div>}
      {task?.status === 'running' && <div className="cd-node-progress"><span style={{ width: `${data.ui.percent}%` }} /></div>}
    </div>}
    <p className="cd-node-prompt">{data.prompt || '点击设置，写下画面描述…'}</p>
    <div className="cd-node-meta"><span>{data.kind === 'text' ? '文本输入' : data.resolution}</span>
      {!!data.versions.length && <button className="nodrag" onClick={data.ui.edit}>v{data.versions.indexOf(version!) + 1} · {data.versions.length} 个版本</button>}</div>
    <footer><button className="cd-node-route nodrag" onClick={data.ui.edit} title="调整此节点的算力"><Cpu size={11} />{data.ui.route}</button>
      {data.kind !== 'text' && <button className={`cd-run nodrag ${busy ? 'cancel' : ''}`} aria-label={`${busy ? '取消' : '生成'} ${data.label}`}
        onClick={busy ? data.ui.cancel : data.ui.run}>{busy ? <Square size={11} /> : <Play size={12} fill="currentColor" />}{busy ? '取消' : '生成'}</button>}</footer>
    <Handle type="source" position={Position.Right} isConnectable={data.kind !== 'video'} />
  </article>
}
const nodeTypes = { creative: CreativeCard }

export default function CanvasDemo({ accounts, visible, refreshAccounts, notify }: {
  accounts: Account[]; visible: boolean; refreshAccounts: (force?: boolean) => Promise<void>; notify: (message: string) => void
}) {
  const accountIds = accounts.map(account => account.id)
  const accountKey = accountIds.join(',')
  const [demo, setDemo] = useState<Demo>(() => loadDemo(accountIds))
  const [quotaRefreshing, setQuotaRefreshing] = useState(false)
  const [now, setNow] = useState(Date.now())
  const flow = useRef<ReactFlowInstance<CreativeNode> | null>(null)
  const surface = useRef<HTMLDivElement>(null)
  const [nodeDialog, setNodeDialog] = useState<{ boardId: string; nodeId: string } | null>(null)
  const [instanceDialog, setInstanceDialog] = useState<Instance | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [boardName, setBoardName] = useState('')
  const [deleting, setDeleting] = useState<string | null>(null)
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuTrigger = useRef<HTMLElement | null>(null)
  const [reset, setReset] = useState(false)
  const [queueFilter, setQueueFilter] = useState('all')
  const [history, setHistory] = useState(false)
  const [showCompute, setShowCompute] = useState(false)
  const board = demo.boards.find(b => b.id === demo.activeId) ?? { id: '', name: '', instanceId: demo.instances[0].id, nodes: [], edges: [] }
  const instance = demo.instances.find(i => i.id === board.instanceId)!
  const active = demo.tasks.filter(unfinished)
  const working = active.length > 0 || demo.instances.some(i => i.phase === 'starting')
  const selected = board.nodes.filter(n => n.selected).map(n => n.id)

  const refreshQuota = useCallback(async (force = false) => {
    setQuotaRefreshing(true)
    try { await refreshAccounts(force) }
    catch { notify('账号额度刷新失败，请稍后重试') }
    finally { setQuotaRefreshing(false) }
  }, [refreshAccounts, notify])
  useEffect(() => { if (visible) void refreshQuota() }, [visible, accountKey, refreshQuota])
  function quotaLabel(id: string) {
    const account = accounts.find(account => account.id === id)
    if (!account) return '未发现账号'
    if (!account.installed) return '未安装入口'
    if (!account.credential_present) return '待登录'
    const balance = account.usage?.balance
    if (account.error) return typeof balance === 'number' ? `${balance.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} CU · 上次记录` : '额度读取失败'
    return typeof balance === 'number' ? `${balance.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} CU` : quotaRefreshing ? '查询中…' : '额度未知'
  }
  function instanceLabel(instance: Instance) {
    return `${instance.name} · ${instance.account} · ${quotaLabel(instance.account)}`
  }

  useEffect(() => {
    if (!menu) return
    menuRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    function outside(event: PointerEvent) {
      if (!menuRef.current?.contains(event.target as globalThis.Node) && !menuTrigger.current?.contains(event.target as globalThis.Node)) setMenu(null)
    }
    function keyboard(event: KeyboardEvent) {
      if (event.key === 'Escape' || event.key === 'Tab') {
        if (event.key === 'Escape') { event.preventDefault(); menuTrigger.current?.focus() }
        setMenu(null)
      }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', keyboard)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', keyboard) }
  }, [menu])

  useEffect(() => {
    try { localStorage.setItem(STORAGE, JSON.stringify(demo)) }
    catch { notify('演示保存失败，请检查浏览器存储空间') }
  }, [demo, notify])
  useEffect(() => {
    if (!working) return
    const advance = () => { const time = Date.now(); setNow(time); setDemo(value => tick(value, time)) }
    advance()
    const timer = window.setInterval(advance, 200)
    return () => window.clearInterval(timer)
  }, [working])

  const run = useCallback((boardId: string, ids: string[]) => {
    setNow(Date.now())
    setDemo(value => enqueue(value, boardId, ids, Date.now()))
  }, [])
  const nodes = useMemo(() => board.nodes.map(node => {
    const task = demo.tasks.find(t => t.boardId === board.id && t.nodeId === node.id && unfinished(t))
      ?? [...demo.tasks].reverse().find(t => t.boardId === board.id && t.nodeId === node.id)
    const routeId = task && unfinished(task) ? task.instanceId : node.data.instanceId || board.instanceId
    const route = demo.instances.find(i => i.id === routeId)?.name ?? '未选择算力'
    return { ...node, data: { ...node.data, ui: { task, percent: task ? progress(task, now) : 0, stage: taskStage(task, now),
      queue: task ? demo.tasks.filter(t => t.instanceId === task.instanceId && t.status === 'queued').findIndex(t => t.id === task.id) + 1 : 0,
      route, run: () => run(board.id, [node.id]), edit: () => setNodeDialog({ boardId: board.id, nodeId: node.id }),
      cancel: () => task && setDemo(value => cancelTask(value, task.id)) } } }
  }), [board, demo.tasks, demo.instances, now, run])

  function newBoard() {
    const next = { id: uid(), name: `画布 ${demo.boards.length + 1}`, instanceId: board.instanceId, nodes: [], edges: [] }
    setDemo(value => ({ ...value, activeId: next.id, boards: [...value.boards, next] }))
  }
  function addNode(kind: Kind) {
    const node = makeNode(kind, board.nodes.length)
    const bounds = surface.current?.getBoundingClientRect()
    if (bounds && flow.current) {
      const center = flow.current.screenToFlowPosition({ x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 })
      node.position = { x: center.x - 133 + board.nodes.length % 3 * 20, y: center.y - 154 + board.nodes.length % 3 * 20 }
    }
    setDemo(value => patchBoard(value, board.id, { nodes: [...board.nodes.map(n => ({ ...n, selected: false })), { ...node, selected: true }] }))
  }
  function connect(connection: Connection) {
    const source = board.nodes.find(n => n.id === connection.source), target = board.nodes.find(n => n.id === connection.target)
    if (!source || !target || source.id === target.id || target.data.kind === 'text'
      || source.data.kind === 'video' || (source.data.kind === 'image' && target.data.kind !== 'video')) {
      notify('可连接文本到图片／视频，或图片到视频'); return
    }
    if (board.edges.some(e => e.source === source.id && e.target === target.id)) return
    setDemo(value => patchBoard(value, board.id, { edges: [...board.edges, { id: uid(), source: source.id, target: target.id, animated: false }] }))
  }
  function deleteSelected() {
    const removable = selected.filter(id => !active.some(t => t.boardId === board.id && t.nodeId === id))
    if (removable.length !== selected.length) notify('生成或排队中的节点请先取消任务')
    setDemo(value => patchBoard(value, board.id, { nodes: board.nodes.filter(n => !removable.includes(n.id)),
      edges: board.edges.filter(e => !removable.includes(e.source) && !removable.includes(e.target)) }))
  }
  function changeDefault(id: string) {
    setDemo(value => patchBoard(value, board.id, { instanceId: id }))
    notify('默认算力已切换，已提交的任务保持原队列')
  }
  function updateInstance(id: string, changes: Partial<Instance>) {
    setDemo(value => ({ ...value, instances: value.instances.map(i => i.id === id ? { ...i, ...changes } : i) }))
  }
  function rename(id: string) {
    setBoardName(demo.boards.find(b => b.id === id)?.name ?? '')
    setRenaming(id); setMenu(null)
  }
  function openMenu(id: string, trigger: HTMLElement, point?: { x: number; y: number }) {
    const bounds = trigger.getBoundingClientRect()
    menuTrigger.current = trigger
    setMenu({ id, x: Math.max(8, Math.min(point?.x ?? bounds.left, window.innerWidth - 208)),
      y: Math.max(8, Math.min(point?.y ?? bounds.bottom + 6, window.innerHeight - 200)) })
  }
  const displayed = demo.tasks.filter(t => (history ? !unfinished(t) : unfinished(t)) && (queueFilter === 'all' || t.instanceId === queueFilter))
  const sorted = history ? [...displayed].reverse() : [...displayed].sort((a, b) => Number(b.status === 'running') - Number(a.status === 'running'))
  const editingBoard = nodeDialog && demo.boards.find(b => b.id === nodeDialog.boardId)
  const editing = editingBoard && editingBoard.nodes.find(n => n.id === nodeDialog!.nodeId)
  const editingBusy = !!nodeDialog && active.some(t => t.boardId === nodeDialog.boardId && t.nodeId === nodeDialog.nodeId)
  const renameBoard = demo.boards.find(b => b.id === renaming)
  const deletingBoard = demo.boards.find(b => b.id === deleting)
  const deleteBlocked = active.some(t => t.boardId === deleting)
  const menuBlocked = active.some(t => t.boardId === menu?.id)

  if (!demo.boards.length) return <section className="canvas-demo cd-empty-start" aria-label="画布列表为空">
    <Layers2 size={38} strokeWidth={1.1} /><h2>还没有画布</h2>
    <button className="primary" onClick={newBoard}><Plus size={15} />新建画布</button>
  </section>

  return <section className={`canvas-demo ${showCompute ? 'show-compute' : ''}`} aria-label="画布交互演示">
    <div className="cd-topbar">
      <nav className="cd-board-tabs" role="tablist" aria-label="画布列表">{demo.boards.map(b => <div key={b.id} className={`cd-board-tab ${b.id === board.id ? 'active' : ''}`}>
        <button role="tab" aria-selected={b.id === board.id} onClick={() => { setMenu(null); setDemo(value => ({ ...value, activeId: b.id })) }} onDoubleClick={() => rename(b.id)}
          onContextMenu={event => { event.preventDefault(); openMenu(b.id, event.currentTarget, { x: event.clientX, y: event.clientY }) }}
          onKeyDown={event => { if ((event.shiftKey && event.key === 'F10') || event.key === 'ContextMenu') { event.preventDefault(); openMenu(b.id, event.currentTarget) } }}>
          <Layers2 size={13} /><span>{b.name}</span>{active.some(t => t.boardId === b.id) && <i />}</button>
        <button className="cd-board-more" aria-label={`画布菜单 ${b.name}`} aria-haspopup="menu" aria-expanded={menu?.id === b.id}
          onClick={event => menu?.id === b.id ? setMenu(null) : openMenu(b.id, event.currentTarget)}><MoreHorizontal size={14} /></button>
      </div>)}
        <button className="icon-button" aria-label="新建画布" onClick={newBoard}><Plus size={16} /></button></nav>
      <div className="cd-top-actions"><span className="cd-demo-badge" title="模拟运行，尚未连接真实 GPU">演示</span>
        <button className="icon-button" aria-label="重置演示" title="重置演示" onClick={() => setReset(true)}><RotateCcw size={15} /></button>
        <button className="secondary" disabled={!selected.some(id => board.nodes.find(n => n.id === id)?.data.kind !== 'text')}
          onClick={() => run(board.id, selected)}><Play size={13} />运行所选{selected.length ? ` ${selected.length}` : ''}</button>
        <button className="primary" disabled={!board.nodes.some(n => n.data.kind !== 'text')}
          onClick={() => run(board.id, board.nodes.map(n => n.id))}><Play size={13} fill="currentColor" />运行画布</button>
      </div>
    </div>
    <div className="cd-body">
      <div className="cd-surface" ref={surface} tabIndex={0} aria-label="节点画布" onKeyDown={event => {
        if ((event.target as HTMLElement).closest('input,textarea,select,[contenteditable=true]')) return
        if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); deleteSelected() }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') {
          event.preventDefault(); setDemo(value => patchBoard(value, board.id, { nodes: board.nodes.map(n => ({ ...n, selected: true })) }))
        }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'd') {
          event.preventDefault()
          const copies = board.nodes.filter(n => selected.includes(n.id)).map(n => ({ ...structuredClone(n), id: uid(),
            position: { x: n.position.x + 36, y: n.position.y + 36 }, selected: true,
            data: { ...structuredClone(n.data), label: n.data.label + ' 副本', versions: [] } }))
          setDemo(value => patchBoard(value, board.id, { nodes: [...board.nodes.map(n => ({ ...n, selected: false })), ...copies] }))
        }
      }}>
        <ReactFlow<CreativeNode> onInit={instance => { flow.current = instance }} key={board.id} nodes={nodes} edges={board.edges} nodeTypes={nodeTypes} fitView={!board.viewport}
          defaultViewport={board.viewport} fitViewOptions={{ padding: .15, maxZoom: 1, nodes: window.innerWidth <= 760 && board.nodes.length ? [{ id: board.nodes[0].id }] : undefined }} minZoom={.2} maxZoom={1.8}
          deleteKeyCode={null} multiSelectionKeyCode="Shift" panActivationKeyCode="Space" panOnDrag={[1, 2]}
          selectionOnDrag zoomOnDoubleClick={false} proOptions={{ hideAttribution: true }}
          onNodesChange={changes => setDemo(value => {
            const latest = value.boards.find(b => b.id === board.id)!
            return patchBoard(value, board.id, { nodes: applyNodeChanges(changes, latest.nodes) })
          })}
          onEdgesChange={changes => setDemo(value => {
            const latest = value.boards.find(b => b.id === board.id)!
            return patchBoard(value, board.id, { edges: applyEdgeChanges(changes, latest.edges) })
          })}
          onNodeDoubleClick={(_, node) => setNodeDialog({ boardId: board.id, nodeId: node.id })}
          onMoveEnd={(_, viewport) => setDemo(value => patchBoard(value, board.id, { viewport }))} onConnect={connect}
          onPaneClick={() => { (document.querySelector('.cd-surface') as HTMLElement | null)?.focus() }}>
          <Background color="#ccd6c5" gap={22} size={1} />
          <Panel position="top-left" className="cd-palette"><button aria-label="添加图片节点" onClick={() => addNode('image')}><ImageIcon size={16} />图片</button>
            <button aria-label="添加视频节点" onClick={() => addNode('video')}><Film size={16} />视频</button>
            <button aria-label="添加文本节点" onClick={() => addNode('text')}><Type size={16} />文本</button>
            {!!selected.length && <><span /><button aria-label="删除所选节点" onClick={deleteSelected}><Trash2 size={15} /></button></>}</Panel>
          <Controls showInteractive={false} />
          {!board.nodes.length && <Panel position="top-center" className="cd-empty-board"><Layers2 size={30} /><strong>从一个画面开始</strong><span>添加图片、视频，或用文本连接它们</span></Panel>}
        </ReactFlow>
        <footer className="cd-board-footer"><button className="cd-rename" onClick={() => rename(board.id)}>{board.name}<Pencil size={11} /></button>
          <span>{board.nodes.length} 个节点</span><span className="cd-shortcut">空格拖动画布 · Shift 多选</span>
          <button className="cd-mobile-compute" onClick={() => setShowCompute(true)}><Cpu size={13} />算力与队列{active.length > 0 && <b>{active.length}</b>}</button></footer>
      </div>
      <aside className="cd-sidebar" aria-label="共享算力与队列">
        <header className="cd-sidebar-heading"><span><Cpu size={16} />共享算力</span><div>
          <button className="icon-button" aria-label="新增演示实例" title="新增演示实例" onClick={() => {
            const next: Instance = { id: uid(), name: `G4 · ${String(demo.instances.length + 1).padStart(2, '0')}`,
              account: accountIds[0] ?? 'colab1', phase: 'stopped', paused: false }
            setDemo(value => ({ ...value, instances: [...value.instances, next] })); setInstanceDialog(next)
          }}><Plus size={16} /></button>
          <button className="icon-button cd-close-compute" aria-label="收起算力面板" onClick={() => setShowCompute(false)}><X size={16} /></button>
        </div></header>
        <label className="cd-default"><span>当前画布默认实例</span><select aria-label="画布默认实例" value={board.instanceId} onChange={e => changeDefault(e.target.value)}>
          {demo.instances.map(i => <option key={i.id} value={i.id}>{instanceLabel(i)}</option>)}</select></label>
        <div className="cd-instances" role="region" aria-label="算力实例列表" tabIndex={0}>{demo.instances.map(i => {
          const tasks = active.filter(t => t.instanceId === i.id), executing = tasks.find(t => t.status === 'running')
          const count = new Set([...demo.boards.filter(b => b.instanceId === i.id || b.nodes.some(n => n.data.instanceId === i.id)).map(b => b.id), ...tasks.map(t => t.boardId)]).size
          return <article key={i.id} className={`cd-instance ${instance.id === i.id ? 'is-default' : ''}`} aria-label={`算力实例 ${i.name}`}>
            <div className="cd-instance-title"><button onClick={() => setInstanceDialog({ ...i })}>{i.name}<Settings2 size={11} /></button>
              <span className={`cd-phase ${i.phase}`}>{i.phase === 'starting' ? <LoaderCircle size={10} className="spin" /> : <i />}
                {i.phase === 'starting' ? '准备中' : i.phase === 'stopped' ? '未启动' : executing ? '运行中' : i.paused ? '队列暂停' : '就绪'}</span></div>
            <div className="cd-instance-meta"><span>{i.account} · RTX PRO 6000</span><span>{count} 张画布</span></div>
            <div className="cd-instance-bottom"><span>{executing ? executing.name : i.phase === 'starting' ? '分配机器 · 加载环境' : tasks.length ? `${tasks.length} 个任务等待调度` : '暂无任务'}</span><div>
              {i.phase === 'stopped' ? <button aria-label={`启动 ${i.name}`} onClick={() => updateInstance(i.id, { phase: 'starting', startedAt: Date.now(), paused: false })}><Power size={12} />启动</button>
                : <><button disabled={i.phase === 'starting'} aria-label={`${i.paused ? '继续' : '暂停'}排队 ${i.name}`}
                  title="只暂停后续调度，当前任务继续" onClick={() => updateInstance(i.id, { paused: !i.paused })}>{i.paused ? <Play size={12} /> : <Pause size={12} />}</button>
                  <button aria-label={`关闭 ${i.name}`} disabled={tasks.length > 0 || i.phase === 'starting'} title={tasks.length ? '请等待队列结束，或先取消／迁移任务' : '关闭演示实例'}
                    onClick={() => updateInstance(i.id, { phase: 'stopped', model: undefined, paused: false })}><Power size={12} /></button></>}
            </div></div>
            {i.phase === 'starting' && <div className="cd-instance-loading"><span style={{ width: `${Math.min(98, (now - (i.startedAt ?? now)) / 2200 * 100)}%` }} /></div>}
          </article>
        })}</div>
        <div className="cd-queue-heading"><strong>任务队列 <small>{active.length}</small></strong><select aria-label="筛选算力队列" value={queueFilter} onChange={e => setQueueFilter(e.target.value)}>
          <option value="all">全部实例</option>{demo.instances.map(i => <option key={i.id} value={i.id}>{i.name}</option>)}</select></div>
        <div className="cd-queue-tabs"><button className={!history ? 'active' : ''} onClick={() => setHistory(false)}>进行中</button>
          <button className={history ? 'active' : ''} onClick={() => setHistory(true)}>历史记录</button>
          <span>{demo.tasks.filter(t => t.status === 'running').length} 路执行</span></div>
        <div className="cd-queue" aria-label="全部画布的任务队列">{sorted.length ? sorted.map(task => {
          const currentBoard = demo.boards.find(b => b.id === task.boardId)
          const route = demo.instances.find(i => i.id === task.instanceId)
          const Icon = icons[task.kind]
          const waiting = task.status === 'queued' && task.dependencies.some(id => demo.tasks.find(t => t.id === id)?.status !== 'done')
          return <article className={`cd-task ${task.status}`} key={task.id} aria-label={`任务 ${task.name}`}>
            <div className="cd-task-main"><span className="cd-task-icon"><Icon size={14} /></span><button disabled={!currentBoard} onClick={() => setDemo(value => ({ ...value, activeId: task.boardId }))}>
              <strong>{task.name}</strong><small>{currentBoard?.name ?? `${task.boardName ?? '画布'}（已删除）`} · {names[task.kind]}</small></button>
              <span className="cd-task-state">{task.status === 'running' && <LoaderCircle size={10} className="spin" />}{task.status === 'done' && <Check size={11} />}
                {waiting ? '等待上游' : taskStage(task, now)}</span>
            </div>
            {task.status === 'running' && <div className="cd-task-progress" role="progressbar" aria-label={`${task.name} 的生成进度`} aria-valuenow={Math.floor(progress(task, now))} aria-valuemin={0} aria-valuemax={100}>
              <span style={{ width: `${progress(task, now)}%` }} /></div>}
            <div className="cd-task-bottom">{task.status === 'queued' ? <select aria-label={`${task.name} 的任务实例`} value={task.instanceId}
              onChange={e => setDemo(value => moveTask(value, task.id, e.target.value, Date.now()))}>{demo.instances.map(i => <option key={i.id} value={i.id}>{instanceLabel(i)}</option>)}</select>
              : <span><Cpu size={10} />{route?.name}</span>}
              <span>{task.status === 'running' ? `${Math.floor(progress(task, now))}%` : task.resolution}</span>
              {unfinished(task) && <button className="icon-button" aria-label={`取消任务 ${task.name}`} onClick={() => setDemo(value => cancelTask(value, task.id))}><X size={12} /></button>}
            </div>
          </article>
        }) : <div className="cd-empty-queue">{history ? <Check size={23} strokeWidth={1.3} /> : <Layers2 size={23} strokeWidth={1.3} />}<strong>{history ? '还没有历史任务' : '队列空闲'}</strong>
          <span>{history ? '运行结果会保留在对应节点' : '多个画布共用实例，任务在这里排队'}</span></div>}</div>
        <footer className="cd-sidebar-footer"><span className="tiny-dot" />单机串行 · 多实例并行</footer>
      </aside>
    </div>
    {editing && editingBoard && <Dialog title="节点设置" close={() => setNodeDialog(null)} className="cd-settings">
      <label className="field"><span>节点名称</span><input aria-label="节点名称" maxLength={60} value={editing.data.label}
        onChange={e => setDemo(value => patchNode(value, editingBoard.id, editing.id, { label: e.target.value }))} /></label>
      <label className="field"><span>{editing.data.kind === 'text' ? '文本内容' : '画面描述'}<small>{modelLabel(editing.data.kind)}</small></span><textarea aria-label="节点描述" rows={3} value={editing.data.prompt}
        onChange={e => setDemo(value => patchNode(value, editingBoard.id, editing.id, { prompt: e.target.value }))} /></label>
      {editing.data.kind !== 'text' && <><label className="field"><span>执行实例</span><select aria-label="节点执行实例" disabled={editingBusy} value={editing.data.instanceId}
        onChange={e => setDemo(value => patchNode(value, editingBoard.id, editing.id, { instanceId: e.target.value }))}>
        <option value="">跟随画布 · {instanceLabel(demo.instances.find(i => i.id === editingBoard.instanceId)!)}</option>{demo.instances.map(i => <option key={i.id} value={i.id}>{instanceLabel(i)}</option>)}</select></label>
        <div className="field-row"><label className="field"><span>画幅与分辨率</span><select aria-label="节点分辨率" value={editing.data.resolution}
          onChange={e => setDemo(value => patchNode(value, editingBoard.id, editing.id, { resolution: e.target.value }))}>
          {(editing.data.kind === 'image' ? ['1:1 · 2K', '16:9 · 2K', '9:16 · 2K', '16:9 · 4K'] : ['16:9 · 768p', '9:16 · 768p', '1:1 · 768p']).map(v => <option key={v}>{v}</option>)}</select></label>
          <label className="field"><span>随机种子</span><input aria-label="节点随机种子" type="number" min={0} step={1} value={editing.data.seed}
            onChange={e => { const seed = Number(e.target.value); if (Number.isSafeInteger(seed) && seed >= 0) setDemo(value => patchNode(value, editingBoard.id, editing.id, { seed })) }} /></label></div></>}
      {!!editing.data.versions.length && <div className="cd-versions"><span>结果版本</span><div>{editing.data.versions.map((version, index) => <button key={version.id}
        aria-label={`使用版本 ${index + 1}`} aria-pressed={(editing.data.activeVersionId ?? editing.data.versions.at(-1)?.id) === version.id}
        onClick={() => setDemo(value => patchNode(value, editingBoard.id, editing.id, { activeVersionId: version.id }))}>v{index + 1}</button>)}</div></div>}
      <footer><span className="subtle">自动保存{editingBusy ? ' · 修改参数仅影响下次运行' : ''}</span><button className="primary" onClick={() => setNodeDialog(null)}>完成</button></footer>
    </Dialog>}
    {instanceDialog && <Dialog title="演示实例" close={() => setInstanceDialog(null)} className="cd-settings">
      <label className="field"><span>实例名称</span><input aria-label="演示实例名称" maxLength={40} value={instanceDialog.name} onChange={e => setInstanceDialog({ ...instanceDialog, name: e.target.value })} /></label>
      <label className="field"><span>账号</span><select aria-label="演示实例账号" value={instanceDialog.account} disabled={instanceDialog.phase !== 'stopped'}
        onChange={e => setInstanceDialog({ ...instanceDialog, account: e.target.value })}>{[...new Set([...accountIds, instanceDialog.account])].map(account => <option key={account} value={account}>{account} · {quotaLabel(account)}</option>)}</select></label>
      <footer><span className="subtle">RTX PRO 6000 · 96 GB · 演示</span><button className="primary" disabled={!instanceDialog.name.trim()} onClick={() => {
        updateInstance(instanceDialog.id, { name: instanceDialog.name.trim(), account: instanceDialog.account }); setInstanceDialog(null)
      }}>保存</button></footer>
    </Dialog>}
    {menu && <div className="cd-board-menu" role="menu" aria-label="画布操作" ref={menuRef} style={{ left: menu.x, top: menu.y }}
      onKeyDown={event => {
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
        event.preventDefault()
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
        const index = items.indexOf(document.activeElement as HTMLButtonElement)
        items[event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus()
      }}>
      <button role="menuitem" onClick={() => rename(menu.id)}><Pencil size={14} />重命名</button>
      <button role="menuitem" onClick={() => { setDemo(value => duplicateBoard(value, menu.id)); setMenu(null); notify('画布副本已创建') }}><Copy size={14} />复制画布</button>
      <div className="cd-menu-divider" />
      <button role="menuitem" className="danger" disabled={menuBlocked} title={menuBlocked ? '请先完成或取消此画布的任务' : '删除此画布'}
        onClick={() => { setDeleting(menu.id); setMenu(null) }}><Trash2 size={14} />删除画布</button>
      {menuBlocked && <p>请先完成或取消此画布的任务</p>}
    </div>}
    {renameBoard && <Dialog title="画布名称" close={() => setRenaming(null)}><form className="cd-board-form" onSubmit={event => {
      event.preventDefault(); if (!boardName.trim()) return
      setDemo(value => patchBoard(value, renameBoard.id, { name: boardName.trim() })); setRenaming(null)
    }}><label className="field"><span>名称</span><input aria-label="画布名称" autoFocus maxLength={60} value={boardName}
      onChange={e => setBoardName(e.target.value)} /></label><footer><button type="button" className="secondary" onClick={() => setRenaming(null)}>取消</button>
        <button className="primary" type="submit" disabled={!boardName.trim()}>保存</button></footer></form></Dialog>}
    {deletingBoard && <Dialog title="删除画布" close={() => setDeleting(null)} className="cd-delete-dialog">
      <p>删除「{deletingBoard.name}」及其中的 {deletingBoard.nodes.length} 个节点？此操作无法撤销。</p>
      <span className="cd-delete-note">{deleteBlocked ? '请先完成或取消此画布的任务。' : demo.boards.length === 1 ? '删除后回到空画布列表。算力实例与任务历史保留。' : '算力实例、其他画布与任务历史保留。'}</span>
      <footer><button className="secondary" autoFocus onClick={() => setDeleting(null)}>取消</button><button className="primary cd-delete-confirm" disabled={deleteBlocked} onClick={() => {
        setDemo(value => deleteBoard(value, deletingBoard.id)); setDeleting(null); notify('画布已删除')
      }}>确认删除</button></footer>
    </Dialog>}
    {reset && <Dialog title="重置画布演示" close={() => setReset(false)}><p>清空演示画布、任务和版本，返回空画布列表。</p><footer><button className="secondary" onClick={() => setReset(false)}>取消</button>
      <button className="primary" onClick={() => { setDemo(initialDemo(accountIds)); setNodeDialog(null); setQueueFilter('all'); setHistory(false); setReset(false) }}>重置演示</button></footer></Dialog>}
  </section>
}
