<script setup lang="ts">
/**
 * DAG 节点输出摘要 / 错误信息展开面板。
 */
import { computed, ref } from 'vue'
import StatusBadge from './StatusBadge.vue'
import { fmtDuration, fmtRelative, fmtTime, prettyJson, statusLabel } from '../../utils/format'
import type { DagNode } from './dag-types'

const props = defineProps<{
  nodes: DagNode[]
}>()

const expanded = ref<Record<string, boolean>>({})

function toggle(name: string) {
  expanded.value[name] = !expanded.value[name]
}

function outputPreview(s?: string | null): string {
  if (!s) return ''
  return prettyJson(s, 2)
}

const ordered = computed(() => props.nodes || [])
</script>

<template>
  <div class="space-y-2">
    <div v-for="node in ordered" :key="node.name" class="card-soft">
      <button
        type="button"
        class="flex w-full items-center justify-between text-left"
        @click="toggle(node.name)"
      >
        <div class="flex min-w-0 items-center gap-2">
          <span class="h-2 w-2 rounded-full"
                :class="node.status === 'success' ? 'bg-emerald-500' : node.status === 'failed' ? 'bg-rose-500' : node.status === 'running' ? 'bg-amber-500 animate-pulse' : 'bg-slate-500'" />
          <p class="truncate text-sm font-semibold text-slate-100">{{ node.name }}</p>
          <span class="text-[11px] text-slate-500">{{ node.agent_role || '—' }}</span>
        </div>
        <div class="flex items-center gap-2">
          <StatusBadge :status="node.status">{{ statusLabel(node.status) }}</StatusBadge>
          <span class="text-xs text-slate-400">{{ expanded[node.name] ? '收起' : '详情' }}</span>
        </div>
      </button>
      <div v-if="expanded[node.name]" class="mt-3 space-y-2 text-xs text-slate-400">
        <div class="grid gap-2 sm:grid-cols-3">
          <div>
            <span class="text-slate-500">模型：</span>
            <span class="font-mono text-slate-300">{{ node.model || '本地' }}</span>
          </div>
          <div>
            <span class="text-slate-500">Token：</span>
            <span class="font-mono text-slate-300">
              {{ node.tokens_in || 0 }} / {{ node.tokens_out || 0 }}
            </span>
          </div>
          <div>
            <span class="text-slate-500">耗时：</span>
            <span class="font-mono text-slate-300">{{ fmtDuration(node.started_at, node.finished_at) }}</span>
          </div>
        </div>
        <div v-if="node.started_at || node.finished_at" class="text-[11px] text-slate-500">
          <span v-if="node.started_at">开始 {{ fmtTime(node.started_at) }} ({{ fmtRelative(node.started_at) }})</span>
          <span v-if="node.finished_at"> · 结束 {{ fmtTime(node.finished_at) }}</span>
        </div>
        <div v-if="node.error">
          <p class="mb-1 text-rose-300">错误：</p>
          <pre class="code-panel max-h-40 whitespace-pre-wrap">{{ outputPreview(node.error) }}</pre>
        </div>
        <div v-else-if="node.output_ref">
          <p class="mb-1 text-slate-500">输出摘要：</p>
          <pre class="code-panel max-h-40 whitespace-pre-wrap">{{ outputPreview(node.output_ref) }}</pre>
        </div>
        <div v-else>
          <p class="text-slate-500">无输出</p>
        </div>
      </div>
    </div>
  </div>
</template>
