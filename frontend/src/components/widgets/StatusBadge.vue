<script setup lang="ts">
/**
 * 通用状态徽标 / 标签。
 */
import { computed } from 'vue'

const props = defineProps<{
  status?: string
  variant?: 'auto' | 'green' | 'amber' | 'red' | 'blue' | 'slate' | 'purple'
}>()

const STATUS_MAP: Record<string, string> = {
  // 成功 / 完成
  success: 'green',
  completed: 'green',
  done: 'green',
  reviewed: 'green',
  review_generated: 'green',
  review_ready: 'green',
  confirmed: 'green',
  uploaded: 'green',
  generated: 'green',
  // 进行中 / 等待
  running: 'amber',
  pending: 'amber',
  processing: 'amber',
  in_progress: 'amber',
  material_ready: 'amber',
  homework_ready: 'amber',
  review_pending: 'amber',
  // 错误 / 失败
  failed: 'red',
  error: 'red',
  rejected: 'red',
  // 草稿 / 占位
  draft: 'slate',
  not_started: 'slate',
  provisional: 'purple',
  // 信息
  info: 'blue',
}

const variantClass = computed(() => {
  const v = props.variant && props.variant !== 'auto'
    ? props.variant
    : STATUS_MAP[(props.status || '').toLowerCase()] || 'slate'
  const map: Record<string, string> = {
    green: 'border-emerald-800/70 bg-emerald-500/15 text-emerald-300',
    amber: 'border-amber-800/70 bg-amber-500/15 text-amber-300',
    red: 'border-rose-800/70 bg-rose-500/15 text-rose-300',
    blue: 'border-blue-800/70 bg-blue-500/15 text-blue-300',
    purple: 'border-purple-800/70 bg-purple-500/15 text-purple-300',
    slate: 'border-slate-700 bg-slate-800/70 text-slate-300',
  }
  return map[v] || map.slate
})
</script>

<template>
  <span
    class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium tracking-wide"
    :class="variantClass"
  >
    <slot>{{ status || '—' }}</slot>
  </span>
</template>
