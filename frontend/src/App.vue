<script setup lang="ts">
import { computed, markRaw, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import DashboardPage from './components/DashboardPage.vue'
import CoursesPage from './components/CoursesPage.vue'
import ChaptersPage from './components/ChaptersPage.vue'
import UploadPage from './components/UploadPage.vue'
import LessonFlowPage from './components/LessonFlowPage.vue'
import HomeworkFlowPage from './components/HomeworkFlowPage.vue'
import ErrorsPage from './components/ErrorsPage.vue'
import ReviewPage from './components/ReviewPage.vue'
import GraphPage from './components/GraphPage.vue'
import ModelsPage from './components/ModelsPage.vue'
import SyncPage from './components/SyncPage.vue'
import RunsPage from './components/RunsPage.vue'
import NotFoundPage from './components/NotFoundPage.vue'
import MobileUploadPage from './components/MobileUploadPage.vue'
import MobileTokenPage from './components/MobileTokenPage.vue'
import ToastHost from './components/widgets/ToastHost.vue'
import { useHealthStore } from './stores/health'

type PageKey =
  | 'dashboard' | 'courses' | 'chapters' | 'upload' | 'lesson-flow'
  | 'homework-flow' | 'errors' | 'review' | 'graph'
  | 'models' | 'sync' | 'runs' | 'm-upload' | 'm-token' | '404'

const KEY_TO_HASH: Record<PageKey, string> = {
  dashboard: '#/',
  courses: '#/courses',
  chapters: '#/chapters',
  upload: '#/upload',
  'lesson-flow': '#/lesson-flow',
  'homework-flow': '#/homework-flow',
  errors: '#/errors',
  review: '#/review',
  graph: '#/graph',
  models: '#/models',
  sync: '#/sync',
  runs: '#/runs',
  'm-upload': '#/m-upload',
  'm-token': '#/m-token',
  '404': '#/404',
}
const HASH_TO_KEY: Record<string, PageKey> = Object.fromEntries(
  Object.entries(KEY_TO_HASH).map(([k, h]) => [h, k as PageKey])
)

const current = ref<PageKey>('dashboard')
const sidebarOpen = ref(false)

const healthStore = useHealthStore()

interface NavItem { key: PageKey; label: string; icon: string }
interface NavGroup { title: string; items: NavItem[] }
const groups: NavGroup[] = [
  {
    title: '今日',
    items: [
      { key: 'dashboard', label: '今日总览', icon: '🗓️' },
      { key: 'errors', label: '待确认错题', icon: '❌' },
    ],
  },
  {
    title: '课程',
    items: [
      { key: 'courses', label: '课程与考试', icon: '📚' },
      { key: 'chapters', label: '章节进度', icon: '🗂️' },
      { key: 'lesson-flow', label: '听课流', icon: '📝' },
    ],
  },
  {
    title: '收件箱',
    items: [
      { key: 'upload', label: '材料收件箱', icon: '📥' },
      { key: 'homework-flow', label: '作业处理', icon: '✏️' },
    ],
  },
  {
    title: '复习',
    items: [
      { key: 'review', label: '复习中心', icon: '🔁' },
    ],
  },
  {
    title: '系统',
    items: [
      { key: 'models', label: '模型与额度', icon: '🤖' },
      { key: 'sync', label: 'Obsidian 同步', icon: '🔄' },
      { key: 'runs', label: '运行日志', icon: '📜' },
      { key: 'm-token', label: '远程访问凭证', icon: '🔐' },
      { key: 'm-upload', label: '移动采集', icon: '📱' },
    ],
  },
]

const pageMap: Record<PageKey, any> = {
  dashboard: markRaw(DashboardPage),
  courses: markRaw(CoursesPage),
  chapters: markRaw(ChaptersPage),
  upload: markRaw(UploadPage),
  'lesson-flow': markRaw(LessonFlowPage),
  'homework-flow': markRaw(HomeworkFlowPage),
  errors: markRaw(ErrorsPage),
  review: markRaw(ReviewPage),
  graph: markRaw(GraphPage),
  models: markRaw(ModelsPage),
  sync: markRaw(SyncPage),
  runs: markRaw(RunsPage),
  'm-upload': markRaw(MobileUploadPage),
  'm-token': markRaw(MobileTokenPage),
  '404': markRaw(NotFoundPage),
}

const activeMeta = computed(() => {
  if (current.value === '404') {
    return { key: '404' as PageKey, label: '页面未找到', icon: '❓' }
  }
  return groups.flatMap((group: NavGroup) => group.items).find((item: NavItem) => item.key === current.value)
})

function normalizeHash(hash: string): string {
  // #/m-upload?token=xxx → #/m-upload（保留 hash 中的 query 用于组件自解析 token）
  if (!hash) return '#/'
  const qIdx = hash.indexOf('?')
  return qIdx >= 0 ? hash.slice(0, qIdx) : hash
}

function setCurrent(key: string) {
  if (key === current.value) return
  // 只允许 PageKey,过滤掉意外值
  if (!(key in pageMap)) return
  current.value = key as PageKey
  const targetHash = KEY_TO_HASH[key as PageKey] || '#/'
  // 保留 hash 中的 query（如 ?token=xxx）,仅当基础 hash 不同才更新
  const baseHash = normalizeHash(window.location.hash)
  if (baseHash !== targetHash) {
    const qIdx = window.location.hash.indexOf('?')
    const query = qIdx >= 0 ? window.location.hash.slice(qIdx) : ''
    window.location.hash = targetHash + query
  }
  sidebarOpen.value = false
}

function syncFromHash() {
  const h = normalizeHash(window.location.hash || '#/')
  const key = HASH_TO_KEY[h]
  current.value = key || '404'
}

function onHashChange() {
  syncFromHash()
}

watch(current, () => {
  // 同步 <title>
  const label = activeMeta.value?.label || '控制台'
  document.title = `${label} · v5 学习控制面板`
})

onMounted(() => {
  // 初次加载:若 URL 没有 hash,补成 #/
  if (!window.location.hash) {
    history.replaceState(null, '', KEY_TO_HASH[current.value])
  } else {
    syncFromHash()
  }
  window.addEventListener('hashchange', onHashChange)
  healthStore.check()
})

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', onHashChange)
})

function goHome() {
  setCurrent('dashboard')
}
</script>

<template>
  <div class="flex min-h-screen bg-slate-950">
    <!-- 移动端遮罩 -->
    <div
      v-if="sidebarOpen"
      class="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm md:hidden"
      @click="sidebarOpen = false"
    />

    <!-- 侧边栏 -->
    <aside
      class="fixed inset-y-0 left-0 z-40 flex h-screen w-64 shrink-0 flex-col border-r border-slate-800/80 bg-slate-950/95 p-4 backdrop-blur-xl transition-transform md:sticky md:top-0 md:translate-x-0"
      :class="sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'"
    >
      <div class="flex items-center justify-between gap-3 px-2 py-3">
        <div class="flex items-center gap-3">
          <div class="grid h-10 w-10 place-items-center rounded-xl bg-blue-600/20 text-lg text-blue-300">🧠</div>
          <div>
            <h1 class="text-sm font-bold tracking-wide text-white">v5 学习 Agent</h1>
            <p class="text-xs text-slate-500">Local Runtime</p>
          </div>
        </div>
        <button
          class="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100 md:hidden"
          aria-label="关闭菜单"
          @click="sidebarOpen = false"
        >✕</button>
      </div>

      <nav class="mt-5 flex-1 space-y-6 overflow-y-auto pb-4">
        <div v-for="group in groups" :key="group.title">
          <p class="px-3 pb-2 text-[11px] font-semibold uppercase tracking-[.14em] text-slate-600">{{ group.title }}</p>
          <div class="space-y-1">
            <button
              v-for="item in group.items"
              :key="item.key"
              @click="setCurrent(item.key)"
              class="group flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              :class="current === item.key
                ? 'bg-blue-600/20 font-semibold text-blue-200 shadow-[inset_0_0_0_1px_rgba(59,130,246,.28)]'
                : 'text-slate-400 hover:bg-slate-900 hover:text-slate-100'"
            >
              <span class="grid h-6 w-6 place-items-center rounded-lg bg-slate-800/70 text-xs">{{ item.icon }}</span>
              <span>{{ item.label }}</span>
            </button>
          </div>
        </div>
      </nav>

      <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-slate-300">后端状态</span>
          <span
            class="inline-flex h-2 w-2 rounded-full"
            :class="healthStore.online ? 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,.8)]' : 'bg-rose-400 shadow-[0_0_10px_rgba(251,113,133,.8)]'"
          />
        </div>
        <p class="mt-1 text-xs" :class="healthStore.online ? 'text-emerald-300' : 'text-rose-300'">
          {{ healthStore.checking ? '检查中...' : healthStore.online ? `在线 · v${healthStore.version || '?'}` : '离线' }}
        </p>
        <p class="mt-1 text-[10px] text-slate-500">v5 后端：127.0.0.1:8800 · 网关用量：127.0.0.1:8080</p>
        <button class="btn btn-ghost mt-2 w-full !px-2 !py-1.5 text-xs" @click="healthStore.check">重新检测</button>
      </div>
    </aside>

    <!-- 主区域 -->
    <main class="h-screen min-w-0 flex-1 overflow-y-auto">
      <div class="mx-auto w-full max-w-[1400px] px-4 py-6 sm:px-6 sm:py-7">
        <header class="mb-6 sm:mb-7">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex min-w-0 items-center gap-3">
              <button
                class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800 md:hidden"
                aria-label="打开菜单"
                @click="sidebarOpen = true"
              >☰</button>
              <div class="min-w-0">
                <p class="text-xs font-semibold uppercase tracking-[.18em] text-blue-400">v5.1 Control Panel</p>
                <h2 class="mt-1 truncate text-xl font-bold text-white sm:text-2xl">{{ activeMeta?.label }}</h2>
              </div>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <span class="chip">网关 8080</span>
              <span class="chip">后端 8800</span>
              <span class="chip">前端 5173</span>
            </div>
          </div>
        </header>
        <component :is="pageMap[current]" @navigate="setCurrent" />
        <div v-if="current === '404'" class="mt-4 text-center">
          <button class="btn btn-secondary" @click="goHome">返回首页</button>
        </div>
      </div>
    </main>

    <!-- Toast 全局容器 -->
    <ToastHost />
  </div>
</template>

