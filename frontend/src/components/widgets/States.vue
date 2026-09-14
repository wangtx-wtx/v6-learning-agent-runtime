<script setup lang="ts">
/**
 * 通用加载/空/错误状态组件。
 * emptyIcon 传 icons.ts 的键会被渲染成矢量图标；传 emoji 则原样显示。
 */
import { computed } from 'vue'
import Icon from './Icon.vue'
import { hasIcon } from './icons'

const props = defineProps<{
  loading?: boolean
  error?: string
  empty?: boolean
  emptyTitle?: string
  emptyHint?: string
  emptyIcon?: string
}>()

defineEmits<{
  (e: 'click', ev: MouseEvent): void
}>()

const useIcon = computed(() => hasIcon(props.emptyIcon))
</script>

<template>
  <div v-if="loading" class="empty">
    <div class="empty-icon">
      <svg class="h-5 w-5 animate-spin text-slate-400" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-opacity="0.22" stroke-width="2.4" />
        <path d="M21 12a9 9 0 0 1-9 9" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" />
      </svg>
    </div>
    <p class="empty-text">加载中…</p>
  </div>

  <div v-else-if="error" class="empty border-rose-500/25 bg-rose-500/[.06]">
    <span class="empty-icon !border-rose-500/25 !bg-rose-500/10 !text-[var(--acc-red)]">
      <Icon name="alert" :size="19" />
    </span>
    <p class="empty-text !text-[var(--acc-red)]">{{ error }}</p>
    <slot name="error" />
  </div>

  <div v-else-if="empty" class="empty">
    <span class="empty-icon">
      <Icon v-if="useIcon" :name="emptyIcon" :size="19" />
      <span v-else class="text-[17px] leading-none">{{ emptyIcon || '📭' }}</span>
    </span>
    <p class="empty-text !text-t2">{{ emptyTitle || '暂无数据' }}</p>
    <p v-if="emptyHint" class="text-[11.5px] leading-5 text-t3">{{ emptyHint }}</p>
    <slot name="empty" />
  </div>

  <div v-else class="contents" @click="$emit('click', $event)">
    <slot />
  </div>
</template>
