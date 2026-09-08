<script setup lang="ts">
/**
 * 通用 KPI 卡片。
 */
defineProps<{
  label: string
  value: number | string
  emoji?: string
  hint?: string
  tone?: 'blue' | 'emerald' | 'amber' | 'rose' | 'violet' | 'slate'
}>()
</script>

<template>
  <div class="stat-card">
    <span class="stat-icon">{{ emoji || '✨' }}</span>
    <p class="stat-label">{{ label }}</p>
    <p class="stat-value">{{ value }}</p>
    <p v-if="hint" class="mt-1 text-xs text-slate-500">{{ hint }}</p>
  </div>
</template>
