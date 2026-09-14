<script setup lang="ts">
/**
 * 通用页面顶部标题区，统一标题、副标题、右侧操作。
 * 传 icon（icons.ts 中的键）显示矢量图标徽章；传 emoji 则降级为字符图标。
 */
import Icon from './Icon.vue'
import { hasIcon } from './icons'

defineProps<{
  title: string
  subtitle?: string
  emoji?: string
  icon?: string
}>()
</script>

<template>
  <section class="page-header">
    <div class="flex items-start gap-3.5">
      <span v-if="icon || emoji" class="icon-badge mt-0.5">
        <Icon v-if="hasIcon(icon)" :name="icon" :size="19" />
        <span v-else class="text-[17px] leading-none">{{ emoji || '•' }}</span>
      </span>
      <div>
        <h2 class="page-title">{{ title }}</h2>
        <p v-if="subtitle" class="page-subtitle">{{ subtitle }}</p>
      </div>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </section>
</template>
