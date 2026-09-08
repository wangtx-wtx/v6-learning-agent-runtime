<script setup lang="ts">
/**
 * DAG 节点状态可视化：横排节点 + 状态点 + 耗时。
 */
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'
import type { DagNode } from './dag-types'
import { statusLabel } from '../../utils/format'

const props = defineProps<{
  nodes: DagNode[]
  loading?: boolean
}>()

function duration(node: DagNode): string {
  if (!node.started_at || !node.finished_at) return '—'
  const t1 = Date.parse(node.started_at)
  const t2 = Date.parse(node.finished_at)
  if (Number.isNaN(t1) || Number.isNaN(t2)) return '—'
  const ms = Math.max(0, t2 - t1)
  if (ms < 1000) return `${ms} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${(ms / 60000).toFixed(1)} min`
}

const statusColor = (s: string) =>
  s === 'success' ? 'bg-emerald-500' :
  s === 'failed' ? 'bg-rose-500' :
  s === 'running' ? 'bg-amber-500 animate-pulse' :
  'bg-slate-500'

const totalTokens = computed(() => {
  return props.nodes.reduce((acc, n) => acc + (Number(n.tokens_in) || 0) + (Number(n.tokens_out) || 0), 0)
})
</script>

<template>
  <div>
    <div v-if="loading" class="empty">
      <span class="empty-icon">⏳</span>
      <p class="empty-text">工作流执行中...</p>
    </div>
    <div v-else>
      <div class="flex flex-wrap items-center gap-2">
        <template v-for="(node, idx) in nodes" :key="node.name">
          <div class="relative">
            <div class="card-soft min-w-[170px] !p-3">
              <div class="flex items-center gap-2">
                <span class="h-2 w-2 rounded-full" :class="statusColor(node.status)"></span>
                <p class="text-sm font-semibold text-slate-100">{{ node.name }}</p>
              </div>
              <p class="mt-1 text-[11px] text-slate-400">
                {{ node.agent_role || '—' }}
              </p>
              <p class="mt-0.5 truncate font-mono text-[11px] text-slate-500" :title="node.model || ''">
                {{ node.model || '本地' }}
              </p>
              <div class="mt-2 flex items-center justify-between text-[11px] text-slate-400">
                <StatusBadge :status="node.status">{{ statusLabel(node.status) }}</StatusBadge>
                <span>{{ duration(node) }}</span>
              </div>
              <p v-if="node.error" class="mt-1 truncate text-[11px] text-rose-300" :title="node.error">
                {{ node.error }}
              </p>
            </div>
          </div>
          <span v-if="idx < nodes.length - 1" class="text-slate-600">→</span>
        </template>
      </div>
      <p v-if="!nodes.length" class="text-xs text-slate-500">暂无节点记录。</p>
      <p v-else-if="totalTokens > 0" class="mt-3 text-[11px] text-slate-500">
        累计 token 用量：{{ totalTokens.toLocaleString() }}
      </p>
    </div>
  </div>
</template>
