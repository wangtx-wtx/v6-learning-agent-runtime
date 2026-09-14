/**
 * 工具函数：解析 JSON、格式化时间、截断。
 */

export function safeJson<T = any>(s?: string | null, fallback: T | null = null): T | null {
  if (!s) return fallback
  try {
    return JSON.parse(s) as T
  } catch {
    return fallback
  }
}

export function truncate(s?: string | null, n = 120): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

export function fmtDate(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const yyyy = d.getFullYear()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

export function fmtRelative(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s).getTime()
  if (Number.isNaN(d)) return s
  const diff = Date.now() - d
  const min = 60 * 1000
  const hr = 60 * min
  const day = 24 * hr
  if (diff < 0) {
    const future = -diff
    if (future < hr) return `${Math.round(future / min)} 分钟后`
    if (future < day) return `${Math.round(future / hr)} 小时后`
    return `${Math.round(future / day)} 天后`
  }
  if (diff < hr) return `${Math.max(1, Math.round(diff / min))} 分钟前`
  if (diff < day) return `${Math.round(diff / hr)} 小时前`
  if (diff < 7 * day) return `${Math.round(diff / day)} 天前`
  return fmtDate(s)
}

export function chapterStatusLabel(s?: string | null): string {
  const map: Record<string, string> = {
    not_started: '未开始',
    in_progress: '进行中',
    completed: '已完成',
    material_ready: '材料已就绪',
    homework_ready: '作业已就绪',
    review_pending: '复习待生成',
    review_generated: '复习已生成',
    reviewed: '已复习',
  }
  return map[(s || '').toLowerCase()] || s || '—'
}

export function chapterStatusTone(s?: string | null): 'slate' | 'amber' | 'blue' | 'green' {
  const map: Record<string, 'slate' | 'amber' | 'blue' | 'green'> = {
    not_started: 'slate',
    in_progress: 'amber',
    material_ready: 'blue',
    homework_ready: 'blue',
    review_pending: 'amber',
    review_generated: 'blue',
    reviewed: 'green',
  }
  return map[(s || '').toLowerCase()] || 'slate'
}

export function reviewStatusLabel(s?: string | null): string {
  const map: Record<string, string> = {
    none: '未生成',
    pending: '待生成',
    generated: '已生成',
    reviewed: '已复习',
  }
  return map[(s || '').toLowerCase()] || s || '—'
}

/**
 * 解析网关用量接口的两种返回结构:
 * 1. 新结构: { quotas: [{ provider, name, windows: [{ label, used, limit, pct, ... }] }] }
 *    (也兼容 { data: { quotas: [...] } } 包装层)
 * 2. 旧/扁平结构: { used_5h, limit_5h, used_7d, limit_7d, used_30d, limit_30d }
 * 返回统一形状的数组,每项含 { label, used, limit, pct, available }.
 */
export interface QuotaWindow {
  label: string
  used: number
  limit: number
  pct: number | null
  available: boolean
}

export function parseUsageWindows(raw: any): {
  windows: QuotaWindow[]
  available: boolean
  provider: string
} {
  const empty = { windows: [], available: false, provider: '' }
  if (!raw || typeof raw !== 'object') return empty

  // 先剥离 data 包装层,新旧结构都兼容
  const data = (raw.data && typeof raw.data === 'object') ? raw.data : raw

  // 新结构: data.quotas[].windows[]
  if (Array.isArray(data.quotas) && data.quotas.length > 0) {
    const q = data.quotas[0] || {}
    const wins: any[] = Array.isArray(q.windows) ? q.windows : []
    const windows: QuotaWindow[] = wins.map((w: any) => {
      const used = Number(w?.used ?? 0)
      const limit = Number(w?.limit ?? 0)
      const pctRaw = Number(w?.pct)
      const pct = Number.isFinite(pctRaw)
        ? pctRaw
        : limit > 0
          ? (used / limit) * 100
          : null
      return {
        label: String(w?.label || ''),
        used,
        limit,
        pct,
        available: true,
      }
    })
    return {
      windows,
      available: windows.length > 0 || !!data.available,
      provider: String(q.provider || ''),
    }
  }

  // 旧扁平结构
  const used5 = Number(data?.used_5h ?? data?.five_hour_usage ?? 0)
  const limit5 = Number(data?.limit_5h ?? data?.five_hour_limit ?? 1200)
  const used7 = Number(data?.used_7d ?? 0)
  const limit7 = Number(data?.limit_7d ?? 9000)
  const used30 = Number(data?.used_30d ?? 0)
  const limit30 = Number(data?.limit_30d ?? 18000)
  const anyUsed = used5 + used7 + used30 > 0
  return {
    windows: [
      { label: '5 小时', used: used5, limit: limit5, pct: limit5 > 0 ? (used5 / limit5) * 100 : null, available: !!data.available || anyUsed },
      { label: '7 天', used: used7, limit: limit7, pct: limit7 > 0 ? (used7 / limit7) * 100 : null, available: true },
      { label: '30 天', used: used30, limit: limit30, pct: limit30 > 0 ? (used30 / limit30) * 100 : null, available: true },
    ],
    available: !!data.available || anyUsed,
    provider: '',
  }
}

/**
 * 通用状态/事件类型 → 中文显示
 */
export function statusLabel(s?: string | null): string {
  const map: Record<string, string> = {
    success: '成功',
    completed: '完成',
    done: '完成',
    // V6 Phase 1: 覆盖门禁未通过 —— 明确区别于「完成」
    degraded: '降级',
    passed: '通过',
    not_used: '未使用',
    included: '已纳入',
    duplicate: '重复',
    noise: '噪声',
    unsupported: '不支持',
    excluded_with_reason: '已排除（有原因）',
    running: '进行中',
    pending: '等待',
    failed: '失败',
    error: '错误',
    confirmed: '已确认',
    provisional: '待确认',
    rejected: '已拒绝',
    synced: '已同步',
    pending_sync: '待同步',
    uploaded: '已上传',
    in_progress: '进行中',
    not_started: '未开始',
    reviewed: '已复习',
    review_generated: '复习已生成',
    review_pending: '复习待生成',
    material_ready: '材料已就绪',
    homework_ready: '作业已就绪',
    available: '可用',
    unavailable: '不可用',
    default_solver: '主链',
    default_reviewer: '审查',
    default_vision: '视觉',
    default_light: '轻量',
    exam: '考试',
    midterm: '期中',
    final: '期末',
    quiz: '测验',
    homework: '作业',
    review: '复习',
    other: '其他',
  }
  return map[(s || '').toLowerCase()] || s || '—'
}

/**
 * 仅显示 HH:mm:ss 的本地时间(配合相对时间使用)
 */
export function fmtTime(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

/**
 * 计算两个 ISO 时间戳之间的可读耗时(如 "12.4 秒"、"3 分 25 秒")。
 */
export function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start || !end) return '—'
  const s = new Date(start).getTime()
  const e = new Date(end).getTime()
  if (!Number.isFinite(s) || !Number.isFinite(e) || e < s) return '—'
  const ms = e - s
  if (ms < 1000) return `${ms} 毫秒`
  const sec = ms / 1000
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 2 : 1)} 秒`
  const min = Math.floor(sec / 60)
  const remSec = Math.round(sec - min * 60)
  return `${min} 分 ${remSec} 秒`
}

/**
 * 安全地将字符串或对象格式化为带缩进的 JSON。空值返回空串。
 */
export function prettyJson(value: unknown, indent = 2): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') {
    // 尝试按 JSON 解析,失败则按原字符串输出
    try {
      const parsed = JSON.parse(value)
      return JSON.stringify(parsed, null, indent)
    } catch {
      return value
    }
  }
  try {
    return JSON.stringify(value, null, indent)
  } catch {
    return String(value)
  }
}

/**
 * 把绝对路径截短为相对友好的展示形式(隐藏用户名 / Desktop 路径前缀)。
 */
export function shortPath(p?: string | null, maxLen = 48): string {
  if (!p) return '—'
  // Windows: C:\Users\<name>\Desktop\新建文件夹 (4)\v5\backend\...
  const m = p.match(/[\\\/]([^\\\/]+\[\\\/][^\\\/]+\[\\\/].*)$/)
  if (m) return '…\\' + m[1]
  if (p.length <= maxLen) return p
  return '…' + p.slice(-maxLen)
}

/**
 * 节点类型英文 → 中文(用于后端返回 topic/default 等未本地化值)。
 */
export function graphTypeLabel(t?: string | null): string {
  const map: Record<string, string> = {
    course: '课程',
    chapter: '章节',
    lesson: '课时',
    knowledge: '知识点',
    topic: '知识点',
    note: '笔记',
    error: '错题',
    default: '默认',
  }
  return map[(t || '').toLowerCase()] || (t || '默认')
}
