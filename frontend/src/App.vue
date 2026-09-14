<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ToastHost from './components/widgets/ToastHost.vue'
import Icon from './components/widgets/Icon.vue'
import GlassControl from './components/widgets/GlassControl.vue'
import { useHealthStore } from './stores/health'
import { useTheme } from './composables/useTheme'

const router = useRouter()
const route = useRoute()
const sidebarOpen = ref(false)

const healthStore = useHealthStore()
const { mode: themeMode, style: appStyle, setThemeMode, setAppStyle } = useTheme()

onMounted(() => {
  healthStore.check()
})

interface NavItem { path: string; label: string; icon: string }
interface NavGroup { title: string; items: NavItem[] }
const groups: NavGroup[] = [
  {
    title: '今日',
    items: [
      { path: '/', label: '今日总览', icon: 'dashboard' },
      { path: '/errors', label: '待确认错题', icon: 'circle-x' },
    ],
  },
  {
    title: '课程',
    items: [
      { path: '/courses', label: '课程与考试', icon: 'book-open' },
      { path: '/calendar', label: '课表与校历', icon: 'calendar' },
      { path: '/chapters', label: '章节进度', icon: 'layers' },
      { path: '/lesson-flow', label: '听课流', icon: 'headphones' },
    ],
  },
  {
    title: '收件箱',
    items: [
      { path: '/upload', label: '材料收件箱', icon: 'inbox' },
      { path: '/homework-flow', label: '作业处理', icon: 'file-pen' },
    ],
  },
  {
    title: '复习',
    items: [
      { path: '/review', label: '复习中心', icon: 'repeat' },
    ],
  },
  {
    title: '系统',
    items: [
      { path: '/models', label: '模型与额度', icon: 'bot' },
      { path: '/sync', label: 'Obsidian 同步', icon: 'refresh' },
      { path: '/runs', label: '运行日志', icon: 'scroll' },
      { path: '/m-token', label: '远程访问凭证', icon: 'key' },
      { path: '/m-upload', label: '移动采集', icon: 'smartphone' },
    ],
  },
]

const activeMeta = computed<NavItem | undefined>(() => {
  const path = route.path === '/404' ? '/404' : route.path
  if (path === '/404') return { path: '/404', label: '页面未找到', icon: 'search-x' }
  return groups.flatMap((g: NavGroup) => g.items).find((i: NavItem) => i.path === path)
})

function navigate(path: string) {
  if (route.path !== path) router.push(path)
  sidebarOpen.value = false
}

const themeOptions = [
  { key: 'light', icon: 'sun', label: '浅色' },
  { key: 'dark', icon: 'moon', label: '深色' },
  { key: 'system', icon: 'monitor', label: '跟随系统' },
] as const

const styleOptions = [
  { key: 'apple', label: '苹果' },
  { key: 'swiss', label: '瑞士' },
  { key: 'broadsheet', label: '报纸' },
] as const
</script>

<template>
  <div class="flex min-h-screen">
    <!-- 移动端遮罩 -->
    <div
      v-if="sidebarOpen"
      class="fixed inset-0 z-30 bg-black/40 backdrop-blur-sm md:hidden"
      @click="sidebarOpen = false"
    />

    <!-- 侧边栏 -->
    <aside
      class="sidebar fixed inset-y-0 left-0 z-40 flex h-screen w-[236px] shrink-0 flex-col transition-transform duration-300 md:sticky md:top-0 md:translate-x-0"
      :class="sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'"
    >
      <!-- 品牌 -->
      <div class="flex items-center justify-between gap-3 px-4 pb-4 pt-5">
        <div class="flex items-center gap-2.5">
          <span class="grid h-8 w-8 shrink-0 place-items-center rounded-[9px] bg-[var(--accent)] text-white">
            <Icon name="brain" :size="17" />
          </span>
          <div class="leading-tight">
            <h1 class="text-[13px] font-semibold tracking-tight text-t1">V6 学习 Agent</h1>
            <p class="mt-0.5 flex items-center gap-1.5 text-[11px] text-t3">
              <span class="led" :class="healthStore.online ? 'text-[var(--acc-green)]' : 'text-[var(--acc-red)]'" />
              Local Runtime
            </p>
          </div>
        </div>
        <button type="button"
          class="rounded-lg p-1.5 text-t3 transition hover:bg-[var(--ctl-fill)] hover:text-t1 md:hidden"
          aria-label="关闭菜单"
          @click="sidebarOpen = false"
        >
          <Icon name="close" :size="16" />
        </button>
      </div>

      <!-- 导航 -->
      <nav class="flex-1 space-y-5 overflow-y-auto px-2.5 pb-3">
        <div v-for="group in groups" :key="group.title">
          <p class="mb-1.5 px-2.5 text-[10.5px] font-semibold uppercase tracking-[.14em] text-t3">
            {{ group.title }}
          </p>
          <div class="space-y-0.5">
            <button type="button"
              v-for="item in group.items"
              :key="item.path"
              class="nav-item group"
              :class="{ 'is-active': route.path === item.path }"
              @click="navigate(item.path)"
            >
              <span class="nav-icon">
                <Icon :name="item.icon" :size="16" />
              </span>
              <span class="truncate">{{ item.label }}</span>
            </button>
          </div>
        </div>
      </nav>

      <!-- 后端状态 -->
      <div class="px-2.5 pb-4">
        <div class="glass-inset rounded-xl p-3">
          <div class="flex items-center justify-between">
            <span class="flex items-center gap-1.5 text-[12px] font-medium text-t2">
              <span class="led" :class="healthStore.online ? 'text-[var(--acc-green)]' : 'text-[var(--acc-red)]'" />
              后端状态
            </span>
            <span
              class="text-[11.5px] font-medium"
              :class="healthStore.online ? 'text-[var(--acc-green)]' : 'text-[var(--acc-red)]'"
            >
              {{ healthStore.checking ? '检查中…' : healthStore.online ? '在线' : '离线' }}
            </span>
          </div>
          <p class="mt-2 text-[11px] leading-5 text-t3">
            <span class="tnum">v{{ healthStore.version || '?' }}</span> · 后端 <span class="tnum">:8800</span><br />
            本地网关 <span class="tnum">:8317</span>
          </p>
          <button type="button" class="btn btn-secondary mt-2.5 w-full !py-1 text-[12px]" @click="healthStore.check">
            <Icon name="refresh" :size="13" />
            重新检测
          </button>
        </div>
      </div>
    </aside>

    <!-- 主区域 -->
    <main class="h-screen min-w-0 flex-1 overflow-y-auto">
      <!-- 吸顶页头 -->
      <header class="topbar sticky top-0 z-20">
        <div class="mx-auto flex w-full max-w-[1440px] items-center justify-between gap-4 px-5 py-3 sm:px-6">
          <div class="flex min-w-0 items-center gap-3">
            <button type="button"
              class="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-[var(--ctl-fill)] text-t2 transition hover:bg-[var(--ctl-fill-hover)] hover:text-t1 md:hidden"
              aria-label="打开菜单"
              @click="sidebarOpen = true"
            >
              <Icon name="menu" :size="17" />
            </button>
            <div class="min-w-0">
              <p class="text-[10.5px] font-semibold uppercase tracking-[.18em] text-[var(--accent)]">
                v5.1 Control Panel
              </p>
              <h2 class="mt-0.5 truncate text-[18px] font-semibold tracking-tight text-t1">
                {{ activeMeta?.label }}
              </h2>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <span class="status-pill">
              <span class="led text-[var(--acc-green)]" />
              网关 <b class="tnum">8317</b>
            </span>
            <span class="status-pill">
              <span class="led" :class="healthStore.online ? 'text-[var(--acc-green)]' : 'text-[var(--acc-red)]'" />
              后端 <b class="tnum">8800</b>
            </span>

            <!-- 主题切换 -->
            <div class="segmented" role="group" aria-label="外观">
              <button type="button"
                v-for="t in themeOptions"
                :key="t.key"
                class="seg-btn"
                :class="{ 'is-on': themeMode === t.key }"
                :title="t.label"
                :aria-label="t.label"
                :aria-pressed="themeMode === t.key"
                @click="setThemeMode(t.key)"
              >
                <Icon :name="t.icon" :size="15" />
              </button>
            </div>

            <!-- 风格档 -->
            <div class="segmented" role="group" aria-label="界面风格">
              <button type="button"
                v-for="s in styleOptions"
                :key="s.key"
                class="seg-btn !w-auto !px-2.5 text-[11.5px] font-medium"
                :class="{ 'is-on': appStyle === s.key }"
                :aria-pressed="appStyle === s.key"
                :title="s.label"
                @click="setAppStyle(s.key)"
              >
                {{ s.label }}
              </button>
            </div>

            <!-- 液态玻璃：模糊强度 -->
            <GlassControl />
          </div>
        </div>
      </header>

      <div class="mx-auto w-full max-w-[1440px] px-5 pb-14 pt-6 sm:px-6">
        <router-view />
      </div>
    </main>

    <!-- Toast 全局容器 -->
    <ToastHost />
  </div>
</template>

<style scoped>
/* 侧边栏：液态玻璃面板（外壳比卡片厚一点，模糊更强） */
.sidebar {
  background:
    linear-gradient(180deg, var(--glass-sheen) 0%, rgba(255, 255, 255, 0) 34%),
    rgb(var(--glass-rgb) / var(--glass-a));
  backdrop-filter: blur(calc(var(--glass-blur) * 1.3)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(calc(var(--glass-blur) * 1.3)) saturate(var(--glass-sat));
  box-shadow: inset -1px 0 0 var(--glass-edge);
  transition: background 0.3s var(--ease);
}

/* 顶栏：更强的毛玻璃 + 底部发丝线 */
.topbar {
  border-bottom: 1px solid var(--line);
  background:
    linear-gradient(180deg, var(--glass-sheen) 0%, rgba(255, 255, 255, 0) 60%),
    rgb(var(--glass-rgb) / calc(var(--glass-a) * 0.92));
  backdrop-filter: blur(calc(var(--glass-blur) * 1.5)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(calc(var(--glass-blur) * 1.5)) saturate(var(--glass-sat));
  transition: background 0.3s var(--ease);
}

/* 导航项 */
.nav-item {
  position: relative;
  display: flex;
  width: 100%;
  align-items: center;
  gap: 9px;
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--txt-1);
  transition: background 0.14s var(--ease), color 0.14s var(--ease);
}
.nav-item:hover { background: var(--ctl-fill); }
.nav-item .nav-icon {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  color: var(--txt-2);
  transition: color 0.14s var(--ease);
}

/* 激活态：Apple 侧边栏 = 实心强调色底 + 白字 */
.nav-item.is-active {
  background: var(--accent);
  color: #fff;
  font-weight: 500;
}
.nav-item.is-active .nav-icon { color: #fff; }

/* 端口状态胶囊 */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--ctl-fill);
  font-size: 11.5px;
  color: var(--txt-2);
  transition: background 0.16s var(--ease);
}
.status-pill:hover { background: var(--ctl-fill-hover); }
.status-pill b { font-weight: 600; color: var(--txt-1); }
.status-pill :deep(.led) { width: 6px; height: 6px; }
</style>
