<script setup lang="ts">
/**
 * 移动端"投喂学习材料"页面
 * 路由: #/m-upload (不暴露在桌面端侧边栏)
 * 功能: 选择课程/章节/课时 → 选择材料类型 → 拍照/录音/选文件 →
 *       逐文件上传 → 一键触发听课/作业/复习流 → 轮询工作流状态
 *
 * 远程访问: 支持 Tailscale Serve / Funnel。
 * - URL 含 ?token=xxx 自动保存到 localStorage 并用于后续请求
 * - 后端启用 V5_MOBILE_TOKEN 时,所有 /api/mobile*、/api/materials/upload、/api/workflows/* 请求必须带 token
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { ChaptersApi, CoursesApi, LessonsApi, MaterialsApi } from '../api/endpoints'
import type { Chapter, Course, Lesson, WorkflowRun } from '../api/endpoints'
import { useToast } from '../composables/useToast'

const toast = useToast()

// 鉴权 token(从 URL ?token= 提取,持久化到 localStorage)
const TOKEN_STORAGE_KEY = 'v5-mobile-token'
function readTokenFromUrl(): string {
  try {
    const hash = window.location.hash || ''
    const queryStart = hash.indexOf('?')
    if (queryStart < 0) return ''
    const qs = new URLSearchParams(hash.slice(queryStart + 1))
    return qs.get('token') || ''
  } catch {
    return ''
  }
}
function loadToken(): string {
  const fromUrl = readTokenFromUrl()
  if (fromUrl) {
    try { localStorage.setItem(TOKEN_STORAGE_KEY, fromUrl) } catch {}
    return fromUrl
  }
  try { return localStorage.getItem(TOKEN_STORAGE_KEY) || '' } catch { return '' }
}
function clearToken() {
  try { localStorage.removeItem(TOKEN_STORAGE_KEY) } catch {}
  toast.info('已清除本地凭证,下次访问需重新输入 token')
}
const mobileToken = ref(loadToken())

// 给所有 api.* / MaterialsApi 调用注入 token header
function authHeaders(): Record<string, string> {
  return mobileToken.value ? { 'x-app-token': mobileToken.value } : {}
}

// 元数据
const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])
const loadingMeta = ref(false)
const metaError = ref('')

// 表单
const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const lessonId = ref<number | ''>('')
type WorkflowType = 'lesson' | 'homework' | 'review'
const workflowType = ref<WorkflowType>('lesson')
const note = ref('')

const workflowTypes: Array<{ key: WorkflowType; label: string; emoji: string }> = [
  { key: 'lesson', label: '听课流', emoji: '📝' },
  { key: 'homework', label: '作业流', emoji: '✏️' },
  { key: 'review', label: '复习流', emoji: '🔁' },
]

// 文件队列
interface MobileFile {
  file: File
  state: 'pending' | 'uploading' | 'success' | 'failed'
  progress: number
  message?: string
  materialId?: number
}
const files = ref<MobileFile[]>([])

// 上传状态
const uploading = ref(false)
const runId = ref<string | number | null>(null)
const runStatus = ref<'idle' | 'running' | 'done' | 'failed'>('idle')

// 联动的可选列表
const filteredChapters = computed(() => {
  if (!courseId.value) return [] as Chapter[]
  return chapters.value.filter(c => c.course_id === Number(courseId.value))
})

const filteredLessons = computed(() => {
  if (!courseId.value) return [] as Lesson[]
  return lessons.value.filter(l => {
    if (l.course_id !== Number(courseId.value)) return false
    if (chapterId.value && l.chapter_id !== Number(chapterId.value)) return false
    return true
  })
})

// 连接信息
interface TailscaleInfo {
  available: boolean
  state: string
  online: boolean
  ip: string | null
  dns_name: string | null
  serve_url: string | null
  funnel_url: string | null
  recommended_url: string | null
}
interface ServerInfo {
  port: number
  lan_ip: string | null
  tailscale_ip: string | null
  frontend_port: number
  all_ips: string[]
  tailscale: TailscaleInfo
  remote_enabled: boolean
  mobile_token_required: boolean
  recommended_url: string | null
}
const serverInfo = ref<ServerInfo | null>(null)
const serverInfoError = ref(false)

// 轮询定时器
let pollTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await Promise.all([loadMeta(), loadServerInfo()])
})

onBeforeUnmount(() => {
  stopPolling()
})

async function loadMeta() {
  loadingMeta.value = true
  metaError.value = ''
  try {
    const headers = authHeaders()
    const [cs, chs, ls] = await Promise.all([
      CoursesApi.list(headers),
      ChaptersApi.list(headers),
      LessonsApi.list(headers),
    ])
    courses.value = cs || []
    chapters.value = chs || []
    lessons.value = ls || []
    if (courses.value.length && !courseId.value) {
      courseId.value = courses.value[0].id
    }
  } catch (e: any) {
    metaError.value = e?.message || String(e) || '加载失败'
  } finally {
    loadingMeta.value = false
  }
}

async function loadServerInfo() {
  try {
    serverInfo.value = await api.get<ServerInfo>('/mobile/server-info', authHeaders())
    serverInfoError.value = false
  } catch {
    serverInfoError.value = true
  }
}

// 章节/课时切换时清空依赖项
function onCourseChange() {
  chapterId.value = ''
  lessonId.value = ''
}
function onChapterChange() {
  lessonId.value = ''
}

// 文件添加(去重:name+size)
function onFilesPicked(ev: Event) {
  const input = ev.target as HTMLInputElement
  if (!input.files || input.files.length === 0) return
  addFiles(input.files)
  // 清空 value,允许再次选择同名文件
  input.value = ''
}

function addFiles(list: FileList | File[]) {
  const arr = Array.from(list)
  const existing = new Set(files.value.map(f => `${f.file.name}:${f.file.size}`))
  let skipped = 0
  for (const f of arr) {
    const key = `${f.name}:${f.size}`
    if (existing.has(key)) {
      skipped++
      continue
    }
    existing.add(key)
    files.value.push({ file: f, state: 'pending', progress: 0 })
  }
  if (skipped > 0) {
    toast.info(`已跳过 ${skipped} 个重复文件`)
  }
}

function removeFile(idx: number) {
  files.value.splice(idx, 1)
}

function fileIcon(name: string): string {
  const lower = name.toLowerCase()
  if (/\.(png|jpe?g|webp|gif|bmp)$/.test(lower)) return '🖼️'
  if (/\.(mp3|wav|m4a|aac|ogg|flac)$/.test(lower)) return '🎵'
  if (lower.endsWith('.pdf')) return '📕'
  if (/\.(pptx?|key)$/.test(lower)) return '📊'
  if (/\.(docx?|rtf)$/.test(lower)) return '📄'
  if (lower.endsWith('.md')) return '📝'
  if (lower.endsWith('.txt')) return '📃'
  return '📎'
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function uploadOneFile(r: MobileFile, attempt = 0): Promise<number> {
  const form = new FormData()
  form.append('file', r.file, r.file.name)
  form.append('course_id', String(courseId.value))
  if (chapterId.value) form.append('chapter_id', String(chapterId.value))
  if (lessonId.value) form.append('lesson_id', String(lessonId.value))

  try {
    r.progress = 30 + attempt * 20
    const result: any = await MaterialsApi.upload(form, authHeaders())
    r.progress = 100
    return Number(result.id)
  } catch (e: any) {
    if (attempt < 1) {
      // 远程网络可能抖动,自动重试 1 次
      r.message = '网络不稳定,重试中…'
      await new Promise(resolve => setTimeout(resolve, 1500))
      return uploadOneFile(r, attempt + 1)
    }
    throw e
  }
}

async function startUploadAndRun() {
  if (!files.value.length) {
    toast.error('请先选择文件')
    return
  }
  if (!courseId.value) {
    toast.error('请选择课程')
    return
  }
  if (!runId.value) runStatus.value = 'idle'

  uploading.value = true
  const uploadedIds: number[] = []

  for (const r of files.value) {
    if (r.state === 'success' && r.materialId) {
      uploadedIds.push(r.materialId)
      continue
    }
    r.state = 'uploading'
    r.progress = 10
    r.message = undefined

    try {
      const id = await uploadOneFile(r)
      r.materialId = id
      r.state = 'success'
      r.message = `已入库 #${id}`
      uploadedIds.push(id)
    } catch (e: any) {
      r.state = 'failed'
      r.progress = 0
      r.message = e?.message || '上传失败'
    }
  }

  const successCount = uploadedIds.length
  const failedCount = files.value.filter(r => r.state === 'failed').length

  if (!successCount) {
    toast.error('全部文件上传失败,未触发工作流')
    uploading.value = false
    return
  }

  toast.success(`${successCount} 个文件上传完成${failedCount > 0 ? `,${failedCount} 个失败` : ''}`)

  // 触发对应工作流
  try {
    const payload: Record<string, unknown> = {
      material_ids: uploadedIds,
      course_id: Number(courseId.value),
    }
    if (chapterId.value) payload.chapter_id = Number(chapterId.value)
    if (lessonId.value) payload.lesson_id = Number(lessonId.value)
    if (note.value) payload.note = note.value

    let endpoint = ''
    if (workflowType.value === 'lesson') endpoint = '/workflows/lesson'
    else if (workflowType.value === 'homework') endpoint = '/workflows/homework'
    else endpoint = '/workflows/review'

    // 听课流需要 transcript 字段,这里用 note 作为占位
    if (workflowType.value === 'lesson') payload.transcript = note.value || '（材料上传触发,无文字转录）'

    const result: any = await api.post(endpoint, payload, authHeaders())
    runId.value = result?.run_id ?? result?.id ?? null
    runStatus.value = 'running'
    toast.success('工作流已启动,正在处理…')
    startPolling()
  } catch (e: any) {
    toast.error(`工作流启动失败:${e?.message || e}`)
    runStatus.value = 'failed'
  }

  uploading.value = false
}

function startPolling() {
  stopPolling()
  if (!runId.value) return
  let ticks = 0
  pollTimer = setInterval(async () => {
    ticks++
    if (ticks > 120) {
      stopPolling()
      runStatus.value = 'failed'
      toast.error('处理超时,请到电脑端查看详情')
      return
    }
    try {
      const r: any = await api.get(`/runs/${runId.value}`, undefined, authHeaders())
      const run: WorkflowRun | undefined = r?.run
      const status = (run?.status || '').toLowerCase()
      if (status === 'completed' || status === 'done' || status === 'success') {
        runStatus.value = 'done'
        stopPolling()
        toast.success('处理完成！')
      } else if (status === 'failed' || status === 'error') {
        runStatus.value = 'failed'
        stopPolling()
        toast.error('处理失败,请到电脑端查看详情')
      }
    } catch {
      // 静默重试
    }
  }, 3000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function resetAll() {
  files.value = []
  runId.value = null
  runStatus.value = 'idle'
  stopPolling()
}

// 复制链接(优先 Clipboard API,iOS 14 以下 fallback)
async function copyUrl(url: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(url)
    } else {
      throw new Error('no clipboard api')
    }
  } catch {
    const ta = document.createElement('textarea')
    ta.value = url
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch {}
    document.body.removeChild(ta)
  }
  toast.success('已复制到剪贴板')
}

// 手动输入 token(用于 Funnel 公网模式)
function setManualToken() {
  const v = window.prompt('请输入移动端凭证 token(后端 V5_MOBILE_TOKEN):')
  if (v === null) return
  const t = v.trim()
  if (!t) {
    clearToken()
    mobileToken.value = ''
  } else {
    try { localStorage.setItem(TOKEN_STORAGE_KEY, t) } catch {}
    mobileToken.value = t
    toast.success('凭证已保存,正在重新加载…')
    loadMeta()
    loadServerInfo()
  }
}
</script>

<template>
  <div class="mx-auto max-w-screen-sm px-4 py-6 text-slate-100">
    <!-- 页头 -->
    <header class="mb-6">
      <p class="text-xs font-semibold uppercase tracking-[.18em] text-blue-400">v5.1 Control Panel</p>
      <h1 class="mt-1 text-2xl font-bold text-white">📤 投喂学习材料</h1>
      <p class="mt-1 text-sm text-slate-400">选好课程和文件,一键上传并触发工作流。</p>
    </header>

    <!-- 元数据加载错误 -->
    <div
      v-if="metaError"
      class="mb-4 rounded-xl border border-rose-700/50 bg-rose-950/40 px-4 py-3 text-sm text-rose-200"
    >
      <p class="font-semibold">加载课程数据失败</p>
      <p class="mt-1 text-xs text-rose-300/80">{{ metaError }}</p>
      <button class="btn btn-secondary mt-2 !py-1.5 text-xs" @click="loadMeta">重试</button>
    </div>

    <!-- 工作流类型 -->
    <section class="mb-5">
      <p class="label mb-2">工作流类型</p>
      <div class="grid grid-cols-3 gap-2">
        <button
          v-for="t in workflowTypes"
          :key="t.key"
          type="button"
          class="min-h-[48px] rounded-xl border px-2 py-3 text-sm font-medium transition active:opacity-80"
          :class="workflowType === t.key
            ? 'border-blue-500 bg-blue-600 text-white shadow-[0_0_0_1px_rgba(59,130,246,.5)]'
            : 'border-slate-700 bg-slate-800 text-slate-300'"
          @click="workflowType = t.key"
        >
          <span class="text-lg">{{ t.emoji }}</span><br />
          {{ t.label }}
        </button>
      </div>
    </section>

    <!-- 课程/章节/课时 -->
    <section class="mb-5 space-y-3">
      <div>
        <label class="label">课程 *</label>
        <select v-model.number="courseId" class="input min-h-[48px] text-base" :disabled="loadingMeta" @change="onCourseChange">
          <option value="" disabled>请选择课程</option>
          <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
        <p v-if="!courses.length && !loadingMeta" class="mt-1 text-xs text-amber-300">
          暂无课程,请先在电脑端添加课程。
        </p>
      </div>

      <div>
        <label class="label">章节（可选）</label>
        <select v-model.number="chapterId" class="input min-h-[48px] text-base" :disabled="!courseId" @change="onChapterChange">
          <option value="">不指定</option>
          <option v-for="ch in filteredChapters" :key="ch.id" :value="ch.id">
            {{ ch.chapter_no ? `第${ch.chapter_no}章` : '' }}{{ ch.title }}
          </option>
        </select>
      </div>

      <div>
        <label class="label">课时（可选）</label>
        <select v-model.number="lessonId" class="input min-h-[48px] text-base" :disabled="!courseId">
          <option value="">不指定</option>
          <option v-for="ls in filteredLessons" :key="ls.id" :value="ls.id">
            {{ ls.lesson_no ? `[${ls.lesson_no}] ` : '' }}{{ ls.title || `课时 #${ls.id}` }}
          </option>
        </select>
      </div>

      <div>
        <label class="label">备注（可选）</label>
        <textarea
          v-model="note"
          rows="2"
          class="input text-base"
          placeholder="如：重点是熵变 / 第 3 题是图像题"
        />
      </div>
    </section>

    <!-- 文件选择 -->
    <section class="mb-5">
      <p class="label mb-2">选择文件</p>
      <div class="space-y-2">
        <label class="flex min-h-[52px] cursor-pointer items-center justify-center rounded-xl border border-slate-700 bg-slate-800 px-4 py-4 text-base font-medium text-slate-200 transition active:bg-slate-700">
          📷 拍照
          <input
            type="file"
            accept="image/*"
            capture="environment"
            class="hidden"
            @change="onFilesPicked"
          />
        </label>
        <label class="flex min-h-[52px] cursor-pointer items-center justify-center rounded-xl border border-slate-700 bg-slate-800 px-4 py-4 text-base font-medium text-slate-200 transition active:bg-slate-700">
          🎤 选择录音
          <input
            type="file"
            accept="audio/*"
            class="hidden"
            @change="onFilesPicked"
          />
        </label>
        <label class="flex min-h-[52px] cursor-pointer items-center justify-center rounded-xl border border-slate-700 bg-slate-800 px-4 py-4 text-base font-medium text-slate-200 transition active:bg-slate-700">
          📁 选择文件（多选）
          <input
            type="file"
            multiple
            accept="image/*,audio/*,.pdf,.ppt,.pptx,.doc,.docx,.md,.txt"
            class="hidden"
            @change="onFilesPicked"
          />
        </label>
      </div>

      <!-- 文件队列 -->
      <ul v-if="files.length" class="mt-3 space-y-2">
        <li
          v-for="(r, idx) in files"
          :key="`${r.file.name}:${r.file.size}:${idx}`"
          class="rounded-lg border border-slate-800 bg-slate-900/60 p-3"
        >
          <div class="flex items-start gap-3">
            <span class="text-2xl">{{ fileIcon(r.file.name) }}</span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-slate-100">{{ r.file.name }}</p>
              <p class="text-xs text-slate-500">{{ fmtSize(r.file.size) }}</p>
              <div class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div
                  class="h-full rounded-full transition-all"
                  :class="r.state === 'failed'
                    ? 'bg-rose-500'
                    : r.state === 'success'
                      ? 'bg-emerald-500'
                      : 'bg-blue-500'"
                  :style="{ width: r.progress + '%' }"
                />
              </div>
              <p
                v-if="r.message"
                class="mt-1 text-xs"
                :class="r.state === 'failed' ? 'text-rose-400' : 'text-emerald-400'"
              >
                {{ r.message }}
              </p>
            </div>
            <button
              v-if="r.state !== 'uploading'"
              class="shrink-0 rounded p-2 text-rose-400 active:text-rose-300"
              aria-label="移除文件"
              @click="removeFile(idx)"
            >✕</button>
          </div>
        </li>
      </ul>
      <p v-else class="mt-3 text-center text-sm text-slate-500">尚未选择文件</p>
    </section>

    <!-- 上传按钮 -->
    <section class="mb-5">
      <button
        type="button"
        class="flex min-h-[56px] w-full items-center justify-center rounded-2xl bg-blue-600 px-4 py-4 text-base font-semibold text-white shadow-lg shadow-blue-900/40 transition active:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        :disabled="uploading || !files.length || !courseId"
        @click="startUploadAndRun"
      >
        <span v-if="uploading">⏳ 处理中…</span>
        <span v-else>📤 上传并处理（{{ files.length }}）</span>
      </button>
      <button
        v-if="files.length || runStatus !== 'idle'"
        type="button"
        class="mt-2 w-full rounded-xl border border-slate-700 bg-slate-900 px-4 py-2.5 text-sm text-slate-300 active:bg-slate-800"
        @click="resetAll"
      >
        🔄 清空重选
      </button>
    </section>

    <!-- 状态反馈 -->
    <section v-if="runStatus !== 'idle' || runId" class="mb-6 space-y-2 rounded-xl border border-slate-800 bg-slate-900/40 p-4 text-sm">
      <p v-if="runStatus === 'running'" class="flex items-center gap-2 text-blue-300">
        <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-blue-400" />
        工作流运行中…（run #{{ runId }}）
      </p>
      <p v-else-if="runStatus === 'done'" class="text-emerald-300">
        ✅ 处理完成！请到电脑端「运行日志」查看详情。
      </p>
      <p v-else-if="runStatus === 'failed'" class="text-rose-300">
        ❌ 处理失败,请到电脑端查看。
      </p>
    </section>

    <!-- 远程连接信息 -->
    <section class="mt-10 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <h3 class="mb-3 text-sm font-semibold text-slate-300">📡 远程访问</h3>

      <!-- Tailscale 在线 + serve 可用 -->
      <template v-if="serverInfo?.tailscale?.available">
        <div class="mb-2 flex items-center gap-2">
          <span class="h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
          <span class="text-sm font-medium text-emerald-300">Tailscale 在线</span>
          <span v-if="serverInfo.tailscale.dns_name" class="text-xs text-slate-500">
            {{ serverInfo.tailscale.dns_name }}
          </span>
        </div>

        <div v-if="serverInfo.tailscale.serve_url" class="mb-2">
          <p class="mb-1 text-xs text-slate-500">HTTPS 地址(同 tailnet 内手机):</p>
          <div class="flex items-center gap-2">
            <code class="flex-1 break-all rounded bg-slate-800 px-2 py-1.5 text-xs text-emerald-300">
              {{ serverInfo.tailscale.serve_url }}
            </code>
            <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="copyUrl(serverInfo.tailscale.serve_url!)">
              复制
            </button>
          </div>
        </div>

        <div v-if="serverInfo.tailscale.funnel_url" class="mb-2">
          <p class="mb-1 text-xs text-amber-400">公网 Funnel 地址(任意设备可访问):</p>
          <div class="flex items-center gap-2">
            <code class="flex-1 break-all rounded bg-slate-800 px-2 py-1.5 text-xs text-amber-300">
              {{ serverInfo.tailscale.funnel_url }}
            </code>
            <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="copyUrl(serverInfo.tailscale.funnel_url!)">
              复制
            </button>
          </div>
        </div>

        <p v-if="serverInfo.mobile_token_required" class="mt-2 rounded bg-amber-950/30 p-2 text-xs text-amber-300">
          ⚠ 当前已启用 Token 鉴权。请在电脑端「远程上传凭证」页生成带 <code>?token=</code> 的专属链接,
          或在本页下方「凭证」区手动输入 token。
        </p>
        <p v-else class="mt-2 text-xs text-slate-500">
          手机需安装 Tailscale App 且登录同一账号;Funnel 模式需在 URL 后加 <code>?token=xxx</code>。
        </p>

        <div v-if="!serverInfo.tailscale.serve_url" class="mt-2 rounded bg-slate-950/50 p-2 text-xs text-amber-300">
          ⚠ Tailscale 已在线但 <code class="text-amber-200">serve</code> 未配置。请在电脑终端运行:<br />
          <code class="mt-1 inline-block rounded bg-slate-800 px-2 py-1 font-mono">tailscale serve --bg 8800</code>
        </div>
      </template>

      <!-- 仅局域网 -->
      <template v-else-if="serverInfo?.lan_ip">
        <div class="mb-2 flex items-center gap-2">
          <span class="h-2 w-2 rounded-full bg-blue-500" />
          <span class="text-sm font-medium text-blue-300">仅局域网可用</span>
        </div>
        <div class="flex items-center gap-2">
          <code class="flex-1 break-all rounded bg-slate-800 px-2 py-1.5 text-xs text-blue-300">
            http://{{ serverInfo.lan_ip }}:{{ serverInfo.frontend_port }}/#/m-upload
          </code>
          <button
            class="btn btn-ghost !px-2 !py-1 text-xs"
            @click="copyUrl(`http://${serverInfo.lan_ip}:${serverInfo.frontend_port}/#/m-upload`)"
          >
            复制
          </button>
        </div>
        <p class="mt-2 text-xs text-slate-500">
          想跨网访问?在电脑运行 <code class="text-amber-300">tailscale up</code> 启用远程模式。
        </p>
      </template>

      <!-- 都不可用 -->
      <template v-else-if="!serverInfoError">
        <p class="text-sm text-amber-300">⚠ 未检测到任何可用连接地址。</p>
        <p class="mt-1 text-xs text-slate-500">
          请确认电脑后端已启动、监听 <code>0.0.0.0</code>,且 Tailscale / 局域网至少一个可用。
        </p>
      </template>

      <p v-else class="text-sm text-amber-300">
        ⚠ 无法获取连接信息(后端可能未启动或未暴露 <code>/api/mobile/server-info</code>)。
      </p>

      <!-- 鉴权 token 入口 -->
      <div v-if="serverInfo?.mobile_token_required || mobileToken" class="mt-4 border-t border-slate-800 pt-3">
        <p class="mb-1 text-xs text-slate-400">
          凭证(token):<span v-if="mobileToken" class="ml-1 text-emerald-300">已设置</span><span v-else class="ml-1 text-amber-300">未设置</span>
        </p>
        <div class="flex gap-2">
          <button class="btn btn-secondary !px-2 !py-1 text-xs" @click="setManualToken">
            {{ mobileToken ? '更换 token' : '手动输入 token' }}
          </button>
          <button v-if="mobileToken" class="btn btn-ghost !px-2 !py-1 text-xs" @click="clearToken">
            清除
          </button>
        </div>
      </div>
    </section>
  </div>
</template>
