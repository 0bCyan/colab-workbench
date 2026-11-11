import { useMemo, useState } from 'react'
import { ReactFlow, Background, Controls, Handle, Position, type NodeProps, type Node, type Edge, type Connection } from '@xyflow/react'
import { Braces, ExternalLink, Plus, X, Boxes, Trash2, Maximize2, Minimize2 } from 'lucide-react'
import '@xyflow/react/dist/style.css'
import { type Draft, type Graph, nodeLabels } from './types'

function Card({ data, selected }: NodeProps<Node<{ label: string; type: string; inputs: string[]; outputs: number; summary: string }>>) {
  return <div className={`flow-card ${selected ? 'selected' : ''}`} style={{ minHeight: Math.max(112, 70 + data.inputs.length * 13) }}>
    <div className="flow-heading"><Boxes size={13} /><span>{data.label}</span></div>
    <div className="flow-class">{data.type}</div>
    <div className="flow-summary">{data.summary}</div>
    {data.inputs.map((name, i) => <Handle key={name} id={name} type="target" position={Position.Left} style={{ top: 55 + i * 13 }} title={name} />)}
    {Array.from({ length: data.outputs }, (_, i) => <Handle key={i} id={String(i)} type="source" position={Position.Right} style={{ top: 55 + i * 17 }} title={`输出 ${i}`} />)}
  </div>
}
const nodeTypes = { card: Card }
const isLink = (v: unknown): v is [string, number] => Array.isArray(v) && v.length === 2 && typeof v[0] === 'string' && typeof v[1] === 'number'

export default function Workflow({ draft, update, nativeUrl, openJson, fail }: { draft: Draft; update: (value: Draft) => void; nativeUrl?: string; openJson: () => void; fail: (message: string) => void }) {
  const [selected, select] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const { nodes, edges } = useMemo(() => {
    const depth: Record<string, number> = {}
    const visit = (id: string, path = new Set<string>()): number => {
      if (id in depth) return depth[id]
      if (path.has(id)) return 0
      const deps = Object.values(draft.graph[id]?.inputs ?? {}).filter(isLink).map(v => v[0]).filter(k => k in draft.graph)
      return depth[id] = deps.length ? Math.max(...deps.map(d => visit(d, new Set([...path, id])))) + 1 : 0
    }
    const rows: Record<number, number> = {}
    const edges: Edge[] = []
    const nodes = Object.entries(draft.graph).map(([id, n]) => {
      const col = visit(id), row = rows[col] ?? 0
      rows[col] = row + 1
      for (const [input, value] of Object.entries(n.inputs)) if (isLink(value)) edges.push({ id: `${id}-${input}`, source: value[0], target: id, sourceHandle: String(value[1]), targetHandle: input, type: 'default', style: { stroke: '#93a6a1', strokeWidth: 1.5 } })
      const usedOutputs = Object.values(draft.graph).flatMap(n => Object.values(n.inputs)).filter(isLink).filter(v => v[0] === id).map(v => v[1] + 1)
      const summary = Object.values(n.inputs).filter(v => !isLink(v)).slice(0, 2).join(' · ')
      return { id, type: 'card', position: draft.positions[id] ?? { x: col * 260, y: row * 160 }, selected: id === selected,
        data: { label: n._meta?.title ?? nodeLabels[n.class_type] ?? n.class_type, type: n.class_type, inputs: Object.keys(n.inputs), outputs: Math.max(1, ...usedOutputs), summary } }
    })
    return { nodes, edges }
  }, [draft, selected])
  const editNode = selected ? draft.graph[selected] : null
  function connect(c: Connection) {
    if (!c.target || !c.source || !c.targetHandle) return
    const graph = structuredClone(draft.graph)
    graph[c.target].inputs[c.targetHandle] = [c.source, Number(c.sourceHandle ?? 0)]
    update({ ...draft, graph })
  }
  function changeInput(field: string, value: unknown) {
    if (!selected) return
    const graph = structuredClone(draft.graph)
    graph[selected].inputs[field] = value
    update({ ...draft, graph })
  }
  function duplicate(type: string) {
    const source = Object.values(draft.graph).find(n => n.class_type === type)!
    const id = String(Math.max(0, ...Object.keys(draft.graph).map(Number).filter(Number.isFinite)) + 1)
    update({ ...draft, graph: { ...draft.graph, [id]: structuredClone(source) } })
    select(id); setAdding(false)
  }
  function remove() {
    if (!selected) return
    if (Object.values(draft.graph).some(n => Object.values(n.inputs).some(v => isLink(v) && v[0] === selected))) return fail('这个节点还有下游连接，请先重新连接下游节点。')
    const graph: Graph = { ...draft.graph }; delete graph[selected]
    update({ ...draft, graph }); select(null)
  }
  return <div className={`workflow-space ${expanded ? 'expanded' : ''}`}>
    <div className="canvas-tools"><span><span className="tiny-dot" /> ComfyUI API · {nodes.length} 节点</span><div>
      <button className="text-button" onClick={() => setAdding(!adding)}><Plus size={14} /> 添加节点</button>
      <button className="text-button" onClick={openJson}><Braces size={14} /> JSON</button>
      {nativeUrl && <a className="text-button" href={nativeUrl} target="_blank" rel="noreferrer">完整编辑器 <ExternalLink size={13} /></a>}
      <button className="icon-button" aria-label={expanded ? '收起画布' : '展开画布'} onClick={() => setExpanded(!expanded)}>{expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
    </div></div>
    {adding && <div className="add-nodes"><strong>从当前工作流复制节点</strong>{[...new Set(Object.values(draft.graph).map(n => n.class_type))].map(type => <button key={type} onClick={() => duplicate(type)}>{nodeLabels[type] ?? type}<Plus size={13} /></button>)}</div>}
    <ReactFlow key={expanded ? 'full' : 'inline'} nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.2} maxZoom={1.6}
      onNodesChange={changes => { const positions = { ...draft.positions }; let changed = false; for (const change of changes) if (change.type === 'position' && change.position) { positions[change.id] = change.position; changed = true } if (changed) update({ ...draft, positions }) }}
      onNodeClick={(_, node) => select(node.id)} onPaneClick={() => select(null)}
      onNodeDragStop={(_, node) => update({ ...draft, positions: { ...draft.positions, [node.id]: node.position } })}
      onConnect={connect} deleteKeyCode={null} proOptions={{ hideAttribution: true }}>
      <Background color="#ced4d0" gap={20} size={1} /><Controls showInteractive={false} />
    </ReactFlow>
    {editNode && selected && <aside className="node-inspector"><header><span>{nodeLabels[editNode.class_type] ?? editNode.class_type}</span><button className="icon-button" onClick={() => select(null)} aria-label="关闭节点属性"><X size={16} /></button></header>
      <code>{editNode.class_type} · #{selected}</code>
      <div className="node-fields">{Object.entries(editNode.inputs).map(([field, value]) => <label key={field}><span>{field}</span>
        {isLink(value) ? <div className="link-value">← 节点 {value[0]} · 输出 {value[1]}</div> : typeof value === 'boolean' ? <input type="checkbox" checked={value} onChange={e => changeInput(field, e.target.checked)} /> : typeof value === 'number' ? <input type="number" value={value} onChange={e => changeInput(field, Number(e.target.value))} /> : <textarea rows={String(value).length > 100 ? 5 : 2} value={String(value ?? '')} onChange={e => changeInput(field, e.target.value)} />}
      </label>)}</div><button className="text-button danger" onClick={remove}><Trash2 size={14} /> 删除节点</button>
    </aside>}
    <div className="canvas-hint">点击节点编辑参数 · 拖动端口连接 · 滚轮缩放</div>
  </div>
}
