<script setup lang="ts">
/**
 * 通用 KPI 卡片。icon 传 icons.ts 的键；emoji 作为降级。
 */
import { computed } from 'vue'
import Icon from './Icon.vue'
import { hasIcon } from './icons'

const props = defineProps<{
  label: string
  value: number | string
  icon?: string
  emoji?: string
  hint?: string
  tone?: 'blue' | 'emerald' | 'amber' | 'rose' | 'violet' | 'slate'
}>()

const toneClass = computed(() => `tone-${props.tone || 'blue'}`)
const useIcon = computed(() => hasIcon(props.icon))
</script>

<template>
  <div class="stat-card" :class="toneClass">
    <span class="stat-icon">
      <Icon v-if="useIcon" :name="icon" :size="17" />
      <span v-else class="text-[15px] leading-none">{{ emoji || '•' }}</span>
    </span>
    <p class="stat-label">{{ label }}</p>
    <p class="stat-value">{{ value }}</p>
    <p v-if="hint" class="mt-1.5 text-[11.5px] leading-5 text-t3">{{ hint }}</p>
  </div>
</template>
