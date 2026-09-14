/**
 * useRunEvents — 运行进度的 SSE 订阅（方案 13.2）。
 *
 * - 优先 EventSource(/api/runs/{id}/events)，实时接收 run/nodes 变化；
 * - 连接失败按指数退避重连（1s→2s→4s→8s→16s，最多 5 次）；
 * - 退避耗尽或浏览器不支持 SSE 时，自动降级为 2s 轮询 RunsApi.get；
 * - run 进入终态（completed/degraded/failed/cancelled）自动停止并回调。
 */
import { onBeforeUnmount, ref } from 'vue'
import { RunsApi } from '../api/endpoints'
import type { RunNode, WorkflowRun } from '../api/endpoints'

export interface RunEventState {
  status: string
  error: string | null
}

// V6 Phase 1: degraded 是终态（覆盖门禁未通过但产物可用），必须一并停止订阅。
const TERMINAL = new Set(['completed', 'degraded', 'failed', 'cancelled'])

export function useRunEvents() {
  const connected = ref(false)          // SSE 是否在线
  const mode = ref<'sse' | 'polling' | 'off'>('off')
  const lastState = ref<RunEventState | null>(null)

  let es: EventSource | null = null
  let pollTimer: number | null = null
  let reconnectTimer: number | null = null
  let attempts = 0
  let runId: number | null = null
  let disposed = false

  function stopAll() {
    if (es) { es.close(); es = null }
    if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null }
    if (reconnectTimer !== null) { window.clearTimeout(reconnectTimer); reconnectTimer = null }
    connected.value = false
    mode.value = 'off'
  }

  function startPolling(id: number, onUpdate: (run: WorkflowRun, nodes: RunNode[]) => void) {
    mode.value = 'polling'
    pollTimer = window.setInterval(async () => {
      try {
        const detail = await RunsApi.get(id)
        onUpdate(detail.run, detail.nodes || [])
        if (TERMINAL.has(detail.run.status)) stop()
      } catch {
        stop()
      }
    }, 2000)
  }

  function connect(id: number, onUpdate: (run: WorkflowRun, nodes: RunNode[]) => void) {
    if (disposed || runId !== id) return
    try {
      es = new EventSource(`/api/runs/${id}/events`)
    } catch {
      startPolling(id, onUpdate)
      return
    }
    es.onopen = () => { connected.value = true; mode.value = 'sse'; attempts = 0 }
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data)
        connected.value = true
        if (payload?.status) {
          onUpdate({ id, status: payload.status, error: payload.error || null } as WorkflowRun,
            (payload.nodes || []) as RunNode[])
        }
        // 终态再拉一次完整轻量 DTO，补齐耗时、token 和输出摘要。
        if (payload && TERMINAL.has(payload.status)) {
          void RunsApi.get(id).then(d => onUpdate(d.run, d.nodes || [])).catch(() => {})
          stop()
        }
      } catch { /* 忽略坏帧 */ }
    }
    es.addEventListener('done', () => { stop() })
    es.onerror = () => {
      connected.value = false
      if (es) { es.close(); es = null }
      if (disposed || runId !== id) return
      attempts += 1
      if (attempts > 5) {
        startPolling(id, onUpdate)
        return
      }
      const delay = Math.min(1000 * 2 ** (attempts - 1), 16000)
      reconnectTimer = window.setTimeout(() => connect(id, onUpdate), delay)
    }
  }

  /** 订阅某次运行；onUpdate 在每次变化时被调用（组件内部刷新 UI）。 */
  function subscribe(id: number, onUpdate: (run: WorkflowRun, nodes: RunNode[]) => void) {
    stopAll()
    disposed = false
    runId = id
    attempts = 0
    // 先立即拉一次，保证 SSE 延迟时 UI 也不空
    void RunsApi.get(id).then(d => onUpdate(d.run, d.nodes || [])).catch(() => {})
    if (typeof EventSource === 'undefined') {
      startPolling(id, onUpdate)
      return
    }
    connect(id, onUpdate)
  }

  function stop() {
    stopAll()
    runId = null
  }

  onBeforeUnmount(() => {
    disposed = true
    stopAll()
  })

  return { subscribe, stop, connected, mode, lastState }
}
