<script setup lang="ts">
/**
 * 通用进度条，支持阈值变色。
 */
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  percent?: number | null
  warnAt?: number
  dangerAt?: number
  label?: string
  showPercent?: boolean
}>(), {
  warnAt: 70,
  dangerAt: 90,
  showPercent: true,
})

const pct = computed(() => {
  if (props.percent === null || props.percent === undefined || Number.isNaN(props.percent)) return null
  return Math.max(0, Math.min(100, Number(props.percent)))
})

const barColor = computed(() => {
  if (pct.value === null) return 'bg-slate-600'
  if (pct.value >= props.dangerAt) return 'bg-rose-500'
  if (pct.value >= props.warnAt) return 'bg-amber-500'
  return 'bg-emerald-500'
})
</script>

<template>
  <div>
    <div v-if="label || showPercent" class="mb-1 flex items-center justify-between text-xs text-slate-400">
      <span v-if="label">{{ label }}</span>
      <span v-if="showPercent" class="font-mono">
        {{ pct === null ? '--' : `${pct.toFixed(1)}%` }}
      </span>
    </div>
    <div class="h-2 w-full overflow-hidden rounded-full bg-slate-800/80">
      <div
        class="h-full rounded-full transition-all duration-500"
        :class="barColor"
        :style="{ width: pct === null ? '8%' : `${pct}%` }"
      />
    </div>
  </div>
</template>
