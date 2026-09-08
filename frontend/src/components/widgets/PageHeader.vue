<script setup lang="ts">
/**
 * 通用页面顶部标题区，统一标题、副标题、右侧操作。
 */
defineProps<{
  title: string
  subtitle?: string
  emoji?: string
}>()
</script>

<template>
  <section class="page-header">
    <div>
      <h2 class="page-title flex items-center gap-2">
        <span v-if="emoji" class="text-2xl">{{ emoji }}</span>
        <span>{{ title }}</span>
      </h2>
      <p v-if="subtitle" class="page-subtitle">{{ subtitle }}</p>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </section>
</template>
