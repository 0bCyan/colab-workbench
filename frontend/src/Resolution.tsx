import { useState } from 'react'

const presets = [
  { value: '1344x768', label: '横屏 · 1344 × 768' },
  { value: '768x1344', label: '竖屏 · 768 × 1344' },
  { value: '1024x1024', label: '方形 · 1024 × 1024' },
  { value: '768x448', label: '快速预览 · 768 × 448' },
]
export const validDimension = (value: number) => Number.isInteger(value) && value >= 256 && value <= 2048 && value % 64 === 0
export const resolutionError = '宽高需为 256–2048 内的 64 的倍数'

export default function Resolution({ width, height, disabled, change }: { width: number; height: number; disabled: boolean; change: (values: { width?: number; height?: number }) => void }) {
  const [custom, setCustom] = useState(false)
  const [touched, setTouched] = useState(false)
  const preset = `${width}x${height}`
  const invalid = !validDimension(width) || !validDimension(height)
  return <div className="resolution-field">
    <div className="resolution-heading"><span>分辨率</span><select aria-label="尺寸预设" disabled={disabled} value={custom || !presets.some(p => p.value === preset) ? 'custom' : preset} onChange={e => {
      setCustom(e.target.value === 'custom'); setTouched(false)
      if (e.target.value !== 'custom') { const [width, height] = e.target.value.split('x').map(Number); change({ width, height }) }
    }}>{presets.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}<option value="custom">自定义尺寸</option></select></div>
    <div className="resolution-inputs">{(['width', 'height'] as const).map((field, i) => <label key={field}><span>{i === 0 ? '宽' : '高'}</span><input aria-label={i === 0 ? '画面宽度' : '画面高度'} aria-describedby="resolution-hint" aria-invalid={touched && !validDimension(field === 'width' ? width : height)} type="number" min={256} max={2048} step={64} value={(field === 'width' ? width : height) || ''} disabled={disabled} onBlur={() => setTouched(true)} onChange={e => { setCustom(true); change({ [field]: Number(e.target.value) }) }} /></label>)}</div>
    <small id="resolution-hint" className={touched && invalid ? 'danger' : ''}>{touched && invalid ? resolutionError : '256–2048 px · 64 的倍数'}</small>
  </div>
}
