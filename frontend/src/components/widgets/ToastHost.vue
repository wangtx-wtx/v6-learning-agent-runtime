<script setup lang="ts">
/**
 * 全局 Toast 容器,渲染 useToast().items。
 * 固定右下角,移动端自适应。
 */
import { useToast } from '../../composables/useToast'

const { items, dismiss } = useToast()

function toneClass(tone: string) {
  switch (tone) {
    case 'success':
      return 'border-emerald-500/40 bg-emerald-950/80 text-emerald-100'
    case 'error':
      return 'border-rose-500/50 bg-rose-950/80 text-rose-100'
    case 'warn':
      return 'border-amber-500/40 bg-amber-950/80 text-amber-100'
    default:
      return 'border-slate-700/60 bg-slate-900/85 text-slate-100'
  }
}

function toneIcon(tone: string) {
  switch (tone) {
    case 'success': return '✓'
    case 'error': return '✕'
    case 'warn': return '⚠'
    default: return 'ℹ'
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
        class="pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border px-4 py-3 shadow-lg backdrop-blur"
        :class="toneClass(t.tone)"
      >
        <span class="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-white/10 text-xs font-bold">
          {{ toneIcon(t.tone) }}
        </span>
        <p class="flex-1 text-sm leading-relaxed">{{ t.message }}</p>
        <button
          class="shrink-0 rounded p-1 text-xs opacity-60 transition hover:bg-white/10 hover:opacity-100"
          aria-label="关闭通知"
          @click="dismiss(t.id)"
        >×</button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 200ms ease;
}
.toast-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.toast-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>