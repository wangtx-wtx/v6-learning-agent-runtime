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
  // V6 Phase 1: 覆盖门禁未通过 —— 产物可用但不得显示为成功
  degraded: 'amber',
  passed: 'green',
  // 材料域 / 覆盖相关
  unsupported: 'slate',
  excluded_with_reason: 'slate',
  not_used: 'slate',
  duplicate: 'slate',
  noise: 'slate',
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
    green: 'sb-green',
    amber: 'sb-amber',
    red: 'sb-red',
    blue: 'sb-blue',
    purple: 'sb-purple',
    slate: 'sb-slate',
  }
  return map[v] || map.slate
})
</script>

<template>
  <span class="sb" :class="variantClass">
    <span class="sb-dot" />
    <slot>{{ status || '—' }}</slot>
  </span>
</template>

<style scoped>
.sb {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px 3px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: 11.5px;
  font-weight: 500;
  letter-spacing: 0.012em;
  line-height: 1.45;
  white-space: nowrap;
}
.sb-dot {
  width: 5px;
  height: 5px;
  border-radius: 999px;
  background: currentColor;
  box-shadow: 0 0 7px 0 currentColor;
  opacity: 0.9;
}

.sb-green { border-color: rgba(16, 185, 129, 0.32); background: rgba(16, 185, 129, 0.12); color: #6ee7b7; }
.sb-amber { border-color: rgba(245, 158, 11, 0.32); background: rgba(245, 158, 11, 0.12); color: #fcd34d; }
.sb-red { border-color: rgba(244, 63, 94, 0.32); background: rgba(244, 63, 94, 0.12); color: #fda4af; }
.sb-blue { border-color: rgba(77, 141, 255, 0.34); background: rgba(77, 141, 255, 0.13); color: #8fb6ff; }
.sb-purple { border-color: rgba(139, 92, 246, 0.34); background: rgba(139, 92, 246, 0.13); color: #c4b5fd; }
.sb-slate { border-color: rgba(148, 163, 184, 0.24); background: rgba(148, 163, 184, 0.1); color: #b3bfd1; }
</style>
