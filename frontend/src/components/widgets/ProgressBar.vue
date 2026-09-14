<script setup lang="ts">
/**
 * 通用进度条，支持阈值变色。
 * 数据缺失（null）显示为虚线占位轨道；0% 显示为空轨道，不再出现"半截灰桩"。
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

const level = computed<'unknown' | 'safe' | 'warn' | 'danger'>(() => {
  if (pct.value === null) return 'unknown'
  if (pct.value >= props.dangerAt) return 'danger'
  if (pct.value >= props.warnAt) return 'warn'
  return 'safe'
})

/** 填充分钟宽度：小于 2% 时给一个最小可见宽度，避免"看不见有没有数据" */
const fillWidth = computed(() => {
  if (pct.value === null || pct.value <= 0) return '0%'
  return `${Math.max(pct.value, 1.5)}%`
})

const percentText = computed(() => {
  if (pct.value === null) return '—'
  // 0 和 100 显示整数更干净
  if (pct.value === 0 || pct.value === 100) return `${pct.value.toFixed(0)}%`
  return pct.value < 10 ? `${pct.value.toFixed(1)}%` : `${pct.value.toFixed(1)}%`
})
</script>

<template>
  <div class="pb-root" :class="`lv-${level}`">
    <div v-if="label || showPercent" class="mb-1.5 flex items-center justify-between gap-3 text-[12px]">
      <span v-if="label" class="text-t3">{{ label }}</span>
      <span v-if="showPercent" class="tnum font-mono font-semibold" :class="`pb-val lv-${level}`">
        {{ percentText }}
      </span>
    </div>
    <div class="pb-track" :class="{ 'is-unknown': level === 'unknown' }">
      <div class="pb-fill" :style="{ width: fillWidth }" />
    </div>
  </div>
</template>

<style scoped>
.pb-track {
  position: relative;
  height: 7px;
  width: 100%;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bar-track);
}

/* 数据缺失：虚线轨道 + 无填充 */
.pb-track.is-unknown {
  background: transparent;
  border: 1px dashed var(--line-strong);
  height: 6px;
}

.pb-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.5s var(--ease);
  background: var(--pb-c, var(--acc-green));
}

.lv-safe { --pb-c: var(--acc-green); }
.lv-warn { --pb-c: var(--acc-orange); }
.lv-danger { --pb-c: var(--acc-red); }

.pb-val.lv-safe { color: var(--acc-green); }
.pb-val.lv-warn { color: var(--acc-orange); }
.pb-val.lv-danger { color: var(--acc-red); }
.pb-val.lv-unknown { color: var(--txt-3); }
</style>
