import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

export default function Dialog({ title, close, children, wide = false, busy = false, className = '' }: { title: string; close: () => void; children: ReactNode; wide?: boolean; busy?: boolean; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null)
  const onClose = useRef(close)
  onClose.current = close
  useEffect(() => {
    const dialog = ref.current!
    const previous = document.activeElement as HTMLElement | null
    dialog.showModal()
    return () => { dialog.close(); previous?.focus() }
  }, [])
  return <dialog ref={ref} className={`modal ${wide ? 'wide' : ''} ${className}`} aria-label={title} onCancel={event => { event.preventDefault(); if (!busy) onClose.current() }} onClick={event => { if (event.target === event.currentTarget && !busy) { const box = event.currentTarget.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close() } }}>
    <header><h2>{title}</h2><button className="icon-button" aria-label="关闭弹窗" disabled={busy} onClick={close}><X size={18} /></button></header>{children}
  </dialog>
}
