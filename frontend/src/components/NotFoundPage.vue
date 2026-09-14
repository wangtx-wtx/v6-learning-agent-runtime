<script setup lang="ts">
import { useRouter } from 'vue-router'
import PageHeader from './widgets/PageHeader.vue'
import Icon from './widgets/Icon.vue'

const router = useRouter()

const suggestions: Array<{ path: string; label: string; icon: string; desc: string }> = [
  { path: '/', label: '总览', icon: 'dashboard', desc: '首页仪表盘' },
  { path: '/courses', label: '课程与考试', icon: 'book-open', desc: '管理课程与考试日历' },
  { path: '/runs', label: '运行日志', icon: 'scroll', desc: '查看最近工作流运行' },
  { path: '/graph', label: '知识图谱', icon: 'network', desc: '浏览知识节点关系' },
]
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="search-x"
      title="页面未找到"
      subtitle="你访问的路径不存在,可能地址输入错误或页面已被移除。"
    />

    <section class="section text-center">
      <p class="text-[72px] font-bold leading-none tracking-tight text-[rgba(77,141,255,.28)]">404</p>
      <p class="mt-3 text-[13px] text-t3">
        当前 URL：<code class="rounded-lg border border-white/10 bg-black/40 px-2 py-1 font-mono text-[12px] text-t2">{{ $route.fullPath }}</code>
      </p>
      <p class="mt-4 text-[13px] text-t3">你可以试试下列入口：</p>
    </section>

    <section class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <button
        v-for="s in suggestions"
        :key="s.path"
        class="card-soft flex flex-col items-start gap-2.5 !p-4 text-left transition hover:-translate-y-0.5 hover:!border-[var(--accent-line)]"
        @click="router.push(s.path)"
      >
        <span class="icon-badge !h-9 !w-9 !rounded-[10px]">
          <Icon :name="s.icon" :size="17" />
        </span>
        <p class="text-[13.5px] font-semibold text-t1">{{ s.label }}</p>
        <p class="text-[12px] leading-5 text-t3">{{ s.desc }}</p>
      </button>
    </section>
  </div>
</template>
