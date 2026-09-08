<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import PageHeader from './widgets/PageHeader.vue'

type NavKey = 'dashboard' | 'courses' | 'runs' | 'graph'
const emit = defineEmits<{ (e: 'navigate', key: NavKey): void }>()

const currentHash = ref<string>('#/404')

function syncHash() {
  currentHash.value = (typeof window !== 'undefined' && window.location?.hash) || '#/404'
}

onMounted(() => {
  syncHash()
  window.addEventListener('hashchange', syncHash)
})

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', syncHash)
})

const suggestions: Array<{ key: NavKey; label: string; icon: string; desc: string }> = [
  { key: 'dashboard', label: '总览', icon: '🎯', desc: '首页仪表盘' },
  { key: 'courses', label: '课程与考试', icon: '📚', desc: '管理课程与考试日历' },
  { key: 'runs', label: '运行日志', icon: '📜', desc: '查看最近工作流运行' },
  { key: 'graph', label: '知识图谱', icon: '🕸️', desc: '浏览知识节点关系' },
]

function navigate(key: NavKey) {
  emit('navigate', key)
}
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="❓"
      title="页面未找到"
      subtitle="你访问的路径不存在,可能地址输入错误或页面已被移除。"
    />

    <section class="section text-center">
      <p class="text-7xl font-bold text-blue-400/40">404</p>
      <p class="mt-2 text-sm text-slate-400">
        当前 URL：<code class="rounded bg-slate-900 px-2 py-1 font-mono text-xs">{{ currentHash }}</code>
      </p>
      <p class="mt-4 text-sm text-slate-500">你可以试试下列入口:</p>
    </section>

    <section class="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <button
        v-for="s in suggestions"
        :key="s.key"
        class="card-soft flex flex-col items-start gap-2 text-left transition hover:border-blue-500/60 hover:bg-blue-950/30"
        @click="navigate(s.key)"
      >
        <span class="text-2xl">{{ s.icon }}</span>
        <p class="text-sm font-semibold text-slate-100">{{ s.label }}</p>
        <p class="text-xs text-slate-500">{{ s.desc }}</p>
      </button>
    </section>
  </div>
</template>