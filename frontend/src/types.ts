export type GraphNode = { class_type: string; inputs: Record<string, unknown>; _meta?: { title?: string } }
export type Graph = Record<string, GraphNode>
export type Draft = { name: string; graph: Graph; positions: Record<string, { x: number; y: number }> }
export type Machine = { phase: string; session?: string; message: string; port?: number; created_at?: number; last_activity?: number; deployment?: { phase: string; file_index?: number }; stats?: unknown }
export type Account = { id: string; label: string; installed: boolean; credential_present: boolean; machine: Machine; usage?: { balance: number | null; rate: number | null; active: number | null }; sessions?: { name: string; hardware: string }[]; error?: string; checked_at?: number }
export type Job = { deleted_at?: number | null; id: string; name: string; account: string; status: string; message: string; created_at: number; started_at?: number; ended_at?: number; progress?: number; stop_after: boolean; outputs: { filename: string; url: string; bytes: number }[]; graph?: Graph }
export type Settings = { identity: string; idle_minutes: number; stop_on_exit: boolean }
export type LLMConfig = { preset_id: string; context_tokens: number; gpu_memory: number; cpu_threads: number }
export type LLMPreset = { id: string; name: string; engine: string; quantization: string; model: string; model_name: string; engine_version: string; size_gib: number; tier: string; reduced_refusal: boolean }
export type LLMService = Machine & { id: string; account: string; config: LLMConfig; endpoint_path: string; model_name: string; active_requests: number }
export type State = { token: string; accounts: Account[]; jobs: Job[]; settings: Settings; drafts: { id: string; name: string; updated_at: number }[]; llm?: { presets: LLMPreset[]; services: LLMService[] } }

export type MetricSample = {
  sampled_at: number
  cpu: { percent: number | null; cores: number }
  memory: { used: number; total: number; percent: number }
  disk: { used: number; total: number; percent: number }
  gpu: { name: string; percent: number | null; memory_used: number | null; memory_total: number | null; memory_percent: number | null; temperature: number | null; power: number | null; power_limit: number | null } | null
  network: { rx_bytes_s: number | null; tx_bytes_s: number | null }
}
export type Metrics = { account: string; session: string | null; status: 'offline' | 'live' | 'unavailable' | 'waiting' | 'disconnected'; sample: MetricSample | null; history: MetricSample[] }

export const activeStatuses = new Set(['pending', 'preparing', 'submitting', 'queued', 'running', 'downloading', 'recovering'])
export const phaseLabel: Record<string, string> = { stopped: '已关闭', checking: '检查会话', provisioning: '申请 GPU', deploying: '部署中', ready: '已就绪', disconnected: '等待重连', error: '需要处理', stopping: '关闭中' }
export const statusLabel: Record<string, string> = { pending: '等待中', preparing: '准备 GPU', submitting: '提交中', queued: '排队中', running: '生成中', downloading: '取回成片', completed: '已完成', cancelled: '已取消', failed: '未完成', recovering: '等待恢复' }
export const nodeLabels: Record<string, string> = { UNETLoader: '扩散模型', CLIPLoader: '文本编码器', VAELoader: 'VAE 模型', MiniMaxH3ImageToVideo: '画面与提示词', MiniMaxH3SigmaShift: '视频 / 音频 Shift', BasicGuider: '条件引导', KSamplerSelect: '采样器', BasicScheduler: '采样步数', RandomNoise: '随机种子', SamplerCustomAdvanced: '生成潜空间', VAEDecode: '视频解码', VAEDecodeAudio: '音频解码', CreateVideo: '合成视频与音频', SaveVideo: '保存视频' }
