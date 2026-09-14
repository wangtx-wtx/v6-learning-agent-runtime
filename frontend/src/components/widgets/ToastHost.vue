<script setup lang="ts">
/**
 * 全局 Toast 容器,渲染 useToast().items。
 * 固定右下角,移动端自适应。
 */
import { useToast } from '../../composables/useToast'
import Icon from './Icon.vue'

const { items, dismiss } = useToast()

function toneClass(tone: string) {
  switch (tone) {
    case 'success':
      return 'border-[var(--acc-green-line)]'
    case 'error':
      return 'border-[var(--acc-red-line)]'
    case 'warn':
      return 'border-[var(--acc-orange-line)]'
    default:
      return 'border-[var(--line)]'
  }
}

function toneIcon(tone: string) {
  switch (tone) {
    case 'success': return 'check'
    case 'error': return 'close'
    case 'warn': return 'alert'
    default: return 'sparkles'
  }
}

function toneIconClass(tone: string) {
  switch (tone) {
    case 'success': return 'text-[var(--acc-green)] bg-[var(--acc-green-soft)]'
    case 'error': return 'text-[var(--acc-red)] bg-[var(--acc-red-soft)]'
    case 'warn': return 'text-[var(--acc-orange)] bg-[var(--acc-orange-soft)]'
    default: return 'text-[var(--accent)] bg-[var(--accent-soft)]'
  }
}
</script>

<template>
  <div
    class="pointer-events-none fixed inset-x-0 bottom-4 z-[1000] flex flex-col items-center gap-2 px-4 sm:bottom-6 sm:items-end sm:px-6"
    role="status"
    aria-live="polite"
  >
    <transition-group name="toast" tag="div" class="flex w-full flex-col items-center gap-2 sm:items-end">
      <div
        v-for="t in items"
        :key="t.id"
        class="glass-panel pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-[14px] border px-3.5 py-3"
        :class="toneClass(t.tone)"
        style="box-shadow: var(--shadow-pop)"
      >
        <span
          class="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-lg border"
          :class="toneIconClass(t.tone)"
        >
          <Icon :name="toneIcon(t.tone)" :size="14" :stroke="2" />
        </span>
        <p class="flex-1 text-[13px] leading-relaxed">{{ t.message }}</p>
        <button
          class="shrink-0 rounded-lg p-1 opacity-55 transition hover:bg-white/10 hover:opacity-100"
          aria-label="关闭通知"
          @click="dismiss(t.id)"
        >
          <Icon name="close" :size="14" />
        </button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 220ms cubic-bezier(0.22, 0.61, 0.36, 1);
}
.toast-enter-from {
  opacity: 0;
  transform: translateY(10px) scale(0.97);
}
.toast-leave-to {
  opacity: 0;
  transform: translateY(-4px) scale(0.98);
}
</style>
