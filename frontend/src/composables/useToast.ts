import { reactive } from 'vue'

export type ToastTone = 'success' | 'error' | 'info' | 'warn'

export interface ToastItem {
  id: number
  tone: ToastTone
  message: string
  /** 自动消失毫秒数,0 表示不自动消失 */
  ttl: number
}

interface ToastState {
  items: ToastItem[]
}

const state = reactive<ToastState>({ items: [] })
let nextId = 1

function push(message: string, tone: ToastTone = 'info', ttl = 3500): number {
  const id = nextId++
  state.items.push({ id, tone, message, ttl })
  if (ttl > 0) {
    setTimeout(() => dismiss(id), ttl)
  }
  return id
}

function dismiss(id: number) {
  const idx = state.items.findIndex(t => t.id === id)
  if (idx >= 0) state.items.splice(idx, 1)
}

export function useToast() {
  return {
    items: state.items,
    success: (msg: string, ttl?: number) => push(msg, 'success', ttl),
    error: (msg: string, ttl?: number) => push(msg, 'error', ttl ?? 5000),
    info: (msg: string, ttl?: number) => push(msg, 'info', ttl),
    warn: (msg: string, ttl?: number) => push(msg, 'warn', ttl),
    dismiss,
  }
}