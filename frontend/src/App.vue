<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ToastHost from './components/widgets/ToastHost.vue'
import { useHealthStore } from './stores/health'

const router = useRouter()
const route = useRoute()
const sidebarOpen = ref(false)

const healthStore = useHealthStore()

onMounted(() => {
  healthStore.check()
})

interface NavItem { path: string; label: string; icon: string }
interface NavGroup { title: string; items: NavItem[] }
const groups: NavGroup[] = [
  {
    title: '今日',
    items: [
      { path: '/', label: '今日总览', icon: '🗓️' },
      { path: '/errors', label: '待确认错题', icon: '❌' },
    ],
  },
  {
    title: '课程',
    items: [
      { path: '/courses', label: '课程与考试', icon: '📚' },
      { path: '/chapters', label: '章节进度', icon: '🗂️' },
      { path: '/lesson-flow', label: '听课流', icon: '📝' },
    ],
  },
  {
    title: '收件箱',
    items: [
      { path: '/upload', label: '材料收件箱', icon: '📥' },
      { path: '/homework-flow', label: '作业处理', icon: '✏️' },
    ],
  },
  {
    title: '复习',
    items: [
      { path: '/review', label: '复习中心', icon: '🔁' },
    ],
  },
  {
    title: '系统',
    items: [
      { path: '/models', label: '模型与额度', icon: '🤖' },
      { path: '/sync', label: 'Obsidian 同步', icon: '🔄' },
      { path: '/runs', label: '运行日志', icon: '📜' },
      { path: '/m-token', label: '远程访问凭证', icon: '🔐' },
      { path: '/m-upload', label: '移动采集', icon: '📱' },
    ],
  },
]

const activeMeta = computed<NavItem | undefined>(() => {
  const path = route.path === '/404' ? '/404' : route.path
  if (path === '/404') return { path: '/404', label: '页面未找到', icon: '❓' }
  return groups.flatMap((g: NavGroup) => g.items).find((i: NavItem) => i.path === path)
})

function navigate(path: string) {
  if (route.path !== path) router.push(path)
  sidebarOpen.value = false
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
              :key="item.path"
              @click="navigate(item.path)"
              class="group flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              :class="route.path === item.path
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
        <router-view />
      </div>
    </main>

    <!-- Toast 全局容器 -->
    <ToastHost />
  </div>
</template>

