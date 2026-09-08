<script setup lang="ts">
/**
 * 通用加载/空/错误状态组件。
 */
defineProps<{
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
</script>

<template>
  <div v-if="loading" class="empty">
    <div class="empty-icon">
      <svg class="h-6 w-6 animate-spin text-slate-400" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-opacity="0.25" stroke-width="2" />
        <path d="M21 12a9 9 0 0 1-9 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
    </div>
    <p class="empty-text">加载中...</p>
  </div>
  <div v-else-if="error" class="empty border-rose-900/40 bg-rose-950/30">
    <span class="empty-icon text-rose-400">⚠️</span>
    <p class="empty-text text-rose-300">{{ error }}</p>
    <slot name="error" />
  </div>
  <div v-else-if="empty" class="empty">
    <span class="empty-icon">{{ emptyIcon || '📭' }}</span>
    <p class="empty-text">{{ emptyTitle || '暂无数据' }}</p>
    <p v-if="emptyHint" class="text-xs text-slate-600">{{ emptyHint }}</p>
    <slot name="empty" />
  </div>
  <div v-else class="contents" @click="$emit('click', $event)">
    <slot />
  </div>
</template>
