/**
 * 统一后端 API 客户端。
 * 通过 Vite proxy(/api → 127.0.0.1:8800)访问。
 * 所有方法均支持可选的 headers 参数,用于移动端鉴权 token 等扩展。
 */
const BASE = '/api'

export interface ApiError extends Error {
  status?: number
  detail?: string
}

type Headers = Record<string, string> | undefined

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data && (data.detail || data.message)) {
        detail = typeof data.detail === 'string' ? data.detail : (data.message || detail)
      }
    } catch {
      try {
        detail = (await res.text()) || detail
      } catch {
        // ignore
      }
    }
    const err: ApiError = new Error(detail)
    err.status = res.status
    err.detail = detail
    throw err
  }
  const text = await res.text()
  if (!text) return undefined as unknown as T
  try {
    return JSON.parse(text) as T
  } catch {
    return undefined as unknown as T
  }
}

function buildQuery(params?: Record<string, unknown>): string {
  if (!params) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    sp.append(k, String(v))
  }
  const qs = sp.toString()
  return qs ? `?${qs}` : ''
}

function mergeHeaders(extra?: Headers): Record<string, string> {
  return extra ? { ...extra } : {}
}

async function get<T>(path: string, params?: Record<string, unknown>, headers?: Headers): Promise<T> {
  const res = await fetch(`${BASE}${path}${buildQuery(params)}`, {
    cache: 'no-store',
    headers: mergeHeaders(headers),
  })
  return handle<T>(res)
}

async function post<T>(path: string, body?: unknown, headers?: Headers): Promise<T> {
  const baseHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { ...baseHeaders, ...mergeHeaders(headers) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(res)
}

async function put<T>(path: string, body?: unknown, headers?: Headers): Promise<T> {
  const baseHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  const res = await fetch(`${BASE}${path}`, {
    method: 'PUT',
    headers: { ...baseHeaders, ...mergeHeaders(headers) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(res)
}

async function patch<T>(path: string, body?: unknown, headers?: Headers): Promise<T> {
  const baseHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  const res = await fetch(`${BASE}${path}`, {
    method: 'PATCH',
    headers: { ...baseHeaders, ...mergeHeaders(headers) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(res)
}

async function del<T>(path: string, headers?: Headers): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'DELETE',
    headers: mergeHeaders(headers),
  })
  return handle<T>(res)
}

async function postForm<T>(path: string, form: FormData, headers?: Headers): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    body: form,
    headers: mergeHeaders(headers),
  })
  return handle<T>(res)
}

export const api = {
  get,
  post,
  put,
  patch,
  del,
  postForm,
}

export default api