<script setup lang="ts">
/**
 * 统一矢量图标组件。传 name（icons.ts 中的键）即可。
 * 未命中时降级为一个小圆点，避免出现空白。
 */
import { computed } from 'vue'
import { iconPath } from './icons'

const props = withDefaults(defineProps<{
  name?: string | null
  size?: number | string
  stroke?: number
}>(), {
  size: 18,
  stroke: 1.7,
})

const inner = computed(() => iconPath(props.name))
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    class="shrink-0"
    aria-hidden="true"
    v-html="inner"
  />
</template>
