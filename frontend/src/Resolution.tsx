import { useState } from 'react'

const presets = [
  { value: '1344x768', label: '横屏 · 1344 × 768' },
  { value: '768x1344', label: '竖屏 · 768 × 1344' },
  { value: '1024x1024', label: '方形 · 1024 × 1024' },
  { value: '768x448', label: '快速预览 · 768 × 448' },
]
const imagePresets = [
  { value: '2048x2048', label: '方形 · 4 MP' },
  { value: '3072x3072', label: '方形 · 9 MP' },
  { value: '4096x4096', label: '方形 · 16 MP' },
  { value: '2688x1536', label: '横屏 · 4 MP' },
  { value: '1536x2688', label: '竖屏 · 4 MP' },
]
export const validDimension = (value: number, image = false) => Number.isInteger(value) && value >= (image ? 1024 : 256) && value <= (image ? 8192 : 2048) && value % (image ? 32 : 64) === 0
export const resolutionError = '宽高需为 256–2048 内的 64 的倍数'
export const imageResolutionError = '宽高需为 1024–8192 内的 32 的倍数，面积 4–20 MP'
export const validResolution = (width: number, height: number, image = false) => validDimension(width, image) && validDimension(height, image) && (!image || (width * height >= 4_000_000 && width * height <= 20_000_000))

export default function Resolution({ width, height, disabled, image = false, change }: { width: number; height: number; disabled: boolean; image?: boolean; change: (values: { width?: number; height?: number }) => void }) {
  const [custom, setCustom] = useState(false)
  const [touched, setTouched] = useState(false)
  const preset = `${width}x${height}`
  const choices = image ? imagePresets : presets
  const invalid = !validResolution(width, height, image)
  return <div className="resolution-field">
    <div className="resolution-heading"><span>分辨率</span><select aria-label="尺寸预设" disabled={disabled} value={custom || !choices.some(p => p.value === preset) ? 'custom' : preset} onChange={e => {
      setCustom(e.target.value === 'custom'); setTouched(false)
      if (e.target.value !== 'custom') { const [width, height] = e.target.value.split('x').map(Number); change({ width, height }) }
    }}>{choices.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}<option value="custom">自定义尺寸</option></select></div>
    <div className="resolution-inputs">{(['width', 'height'] as const).map((field, i) => <label key={field}><span>{i === 0 ? '宽' : '高'}</span><input aria-label={i === 0 ? '画面宽度' : '画面高度'} aria-describedby="resolution-hint" aria-invalid={touched && invalid} type="number" min={image ? 1024 : 256} max={image ? 8192 : 2048} step={image ? 32 : 64} value={(field === 'width' ? width : height) || ''} disabled={disabled} onBlur={() => setTouched(true)} onChange={e => { setCustom(true); change({ [field]: Number(e.target.value) }) }} /></label>)}</div>
    <small id="resolution-hint" className={touched && invalid ? 'danger' : ''}>{touched && invalid ? image ? imageResolutionError : resolutionError : image ? `${(width * height / 1e6).toFixed(1)} MP · 32 的倍数` : '256–2048 px · 64 的倍数'}</small>
  </div>
}
