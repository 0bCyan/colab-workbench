import type { Edge, Node, Viewport } from '@xyflow/react'

export type Kind = 'image' | 'video' | 'text'
export type Version = { id: string; prompt: string; seed: number; at: number }
export type CardData = Record<string, unknown> & {
  label: string; kind: Kind; prompt: string; instanceId: string; resolution: string; seed: number; versions: Version[]
}
export type CreativeNode = Node<CardData, 'creative'>
export type Board = { id: string; name: string; instanceId: string; nodes: CreativeNode[]; edges: Edge[]; viewport?: Viewport }
export type Instance = { id: string; name: string; account: string; phase: 'ready' | 'stopped' | 'starting'; paused: boolean; startedAt?: number; model?: Kind }
export type Task = {
  id: string; boardId: string; nodeId: string; instanceId: string; name: string; kind: Kind;
  prompt: string; seed: number; resolution: string; dependencies: string[];
  status: 'queued' | 'running' | 'done' | 'cancelled'; createdAt: number; startedAt?: number; duration?: number; loading?: boolean
}
export type Demo = { version: 1; activeId: string; boards: Board[]; instances: Instance[]; tasks: Task[] }
export const STORAGE = 'fieldwork-canvas-demo-v1'
export const uid = () => crypto.randomUUID()
export const unfinished = (task: Task) => task.status === 'queued' || task.status === 'running'
export const names: Record<Kind, string> = { image: '图片', video: '视频', text: '文本' }
export const progress = (task: Task, now: number) => task.status === 'done' ? 100 : task.status === 'running'
  ? Math.min(99, Math.max(0, (now - (task.startedAt ?? now)) / (task.duration ?? 8000) * 100)) : 0

export function makeNode(kind: Kind, index: number, prompt = '', label?: string): CreativeNode {
  return { id: uid(), type: 'creative', position: { x: (index % 3) * 306, y: Math.floor(index / 3) * 345 },
    data: { label: label ?? `${names[kind]} ${index + 1}`, kind, prompt, instanceId: '',
      resolution: kind === 'video' ? '16:9 · 768p' : '1:1 · 2K', seed: Math.floor(Math.random() * 2 ** 32), versions: [] } }
}
export function initialDemo(accounts: string[]): Demo {
  const instances: Instance[] = [
    { id: 'demo-g4-1', name: 'G4 · 01', account: accounts[0] ?? 'colab1', phase: 'ready', paused: false },
    { id: 'demo-g4-2', name: 'G4 · 02', account: accounts[1] ?? accounts[0] ?? 'colab1', phase: 'stopped', paused: false },
  ]
  const boards: Board[] = [
    { id: 'demo-images', name: '图片探索', instanceId: instances[0].id, edges: [], nodes: [
      makeNode('image', 0, '晨雾中的山野，柔和的日光，安静的绿色。', '晨雾'),
      makeNode('image', 1, '湖畔远山，水面倒影，淡雅的自然色彩。', '远山'),
      makeNode('image', 2, '树影与微风，午后光线穿过层叠的叶片。', '树影'),
    ] },
    { id: 'demo-video', name: '视频分镜', instanceId: instances[0].id, edges: [], nodes: [
      makeNode('video', 0, '镜头缓缓掠过森林，薄雾在晨光中流动。', '森林漫游'),
      makeNode('video', 1, '固定镜头，湖面涟漪轻轻扩散，远处云层移动。', '湖面涟漪'),
    ] },
  ]
  return { version: 1, activeId: boards[0].id, instances, boards, tasks: [] }
}
export function loadDemo(accounts: string[]): Demo {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE) ?? 'null') as Demo | null
    if (value?.version === 1 && Array.isArray(value.boards) && value.boards.length && Array.isArray(value.tasks)
      && Array.isArray(value.instances) && value.instances.length && value.boards.some(b => b.id === value.activeId)
      && value.boards.every(b => Array.isArray(b.nodes) && Array.isArray(b.edges)
        && value.instances.some(i => i.id === b.instanceId)
        && b.nodes.every(n => n.data && ['text', 'image', 'video'].includes(n.data.kind) && Array.isArray(n.data.versions)))) return value
  } catch { /* A corrupt demo cannot affect the actual workbench. */ }
  return initialDemo(accounts)
}
export function patchBoard(demo: Demo, id: string, changes: Partial<Board>): Demo {
  return { ...demo, boards: demo.boards.map(b => b.id === id ? { ...b, ...changes } : b) }
}
export function patchNode(demo: Demo, boardId: string, nodeId: string, data: Partial<CardData>): Demo {
  const board = demo.boards.find(b => b.id === boardId)!
  return patchBoard(demo, boardId, { nodes: board.nodes.map(n => n.id === nodeId ? { ...n, data: { ...n.data, ...data } } : n) })
}
export function enqueue(demo: Demo, boardId: string, selected: string[], now: number): Demo {
  const board = demo.boards.find(b => b.id === boardId)!
  const pending = new Map(demo.tasks.filter(t => t.boardId === boardId && unfinished(t)).map(t => [t.nodeId, t.id]))
  const targets = new Set(selected)
  const created: Task[] = [], visiting = new Set<string>()
  const visit = (id: string): string | undefined => {
    const node = board.nodes.find(n => n.id === id)
    if (!node || node.data.kind === 'text') return
    if (pending.has(id)) return pending.get(id)
    if (!targets.has(id) && node.data.versions.length) return
    if (visiting.has(id)) throw new Error('节点连接存在循环')
    visiting.add(id)
    const upstream = board.edges.filter(e => e.target === id).map(e => e.source)
    const dependencies = upstream.map(visit).filter((id): id is string => !!id)
    const text = upstream.map(id => board.nodes.find(n => n.id === id)).filter(n => n?.data.kind === 'text').map(n => n!.data.prompt)
    const task: Task = { id: uid(), boardId, nodeId: id, instanceId: node.data.instanceId || board.instanceId,
      name: node.data.label, kind: node.data.kind, prompt: [...text, node.data.prompt].filter(Boolean).join('\n'),
      seed: node.data.seed, resolution: node.data.resolution, dependencies, status: 'queued', createdAt: now }
    created.push(task); pending.set(id, task.id); visiting.delete(id)
    return task.id
  }
  selected.forEach(visit)
  if (!created.length) return demo
  const used = new Set(created.map(t => t.instanceId))
  return { ...demo, tasks: [...demo.tasks, ...created], instances: demo.instances.map(i => used.has(i.id) && i.phase === 'stopped'
    ? { ...i, phase: 'starting', startedAt: now, paused: false } : i) }
}

export function tick(demo: Demo, now: number): Demo {
  let changed = false
  const tasks = demo.tasks.map(t => ({ ...t }))
  const instances = demo.instances.map(i => ({ ...i }))
  let boards = demo.boards
  for (const instance of instances) {
    if (instance.phase === 'starting' && now - (instance.startedAt ?? now) >= 2200) {
      instance.phase = 'ready'; changed = true
    }
    const running = tasks.find(t => t.instanceId === instance.id && t.status === 'running')
    if (running && now - running.startedAt! >= running.duration!) {
      running.status = 'done'; changed = true
      boards = boards.map(board => board.id !== running.boardId ? board : { ...board, nodes: board.nodes.map(node => node.id !== running.nodeId ? node : {
        ...node, data: { ...node.data, activeVersionId: running.id, versions: [...node.data.versions,
          { id: running.id, prompt: running.prompt, seed: running.seed, at: now }] },
      }) })
    }
    for (const task of tasks.filter(t => t.instanceId === instance.id && t.status === 'queued')) {
      if (task.dependencies.some(id => tasks.find(t => t.id === id)?.status === 'cancelled')) {
        task.status = 'cancelled'; changed = true
      }
    }
    if (instance.phase !== 'ready' || instance.paused || tasks.some(t => t.instanceId === instance.id && t.status === 'running')) continue
    const next = tasks.find(t => t.instanceId === instance.id && t.status === 'queued'
      && t.dependencies.every(id => tasks.find(t => t.id === id)?.status === 'done'))
    if (next) {
      next.status = 'running'; next.startedAt = now; next.loading = instance.model !== next.kind
      next.duration = (next.kind === 'video' ? 10500 : 6500) + (next.loading ? 1600 : 0)
      instance.model = next.kind; changed = true
    }
  }
  return changed ? { ...demo, instances, tasks, boards } : demo
}
export function moveTask(demo: Demo, taskId: string, instanceId: string, now: number): Demo {
  const target = demo.instances.find(i => i.id === instanceId)
  if (!target || !demo.tasks.some(t => t.id === taskId && t.status === 'queued')) return demo
  return { ...demo, tasks: demo.tasks.map(t => t.id === taskId ? { ...t, instanceId } : t),
    instances: demo.instances.map(i => i.id === instanceId && i.phase === 'stopped' ? { ...i, phase: 'starting', startedAt: now, paused: false } : i) }
}
export function cancelTask(demo: Demo, taskId: string): Demo {
  const cancelled = new Set([taskId])
  let previous = -1
  while (cancelled.size !== previous) {
    previous = cancelled.size
    for (const task of demo.tasks) if (unfinished(task) && task.dependencies.some(id => cancelled.has(id))) cancelled.add(task.id)
  }
  return { ...demo, tasks: demo.tasks.map(t => cancelled.has(t.id) && unfinished(t) ? { ...t, status: 'cancelled' } : t) }
}
