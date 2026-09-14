<script setup lang="ts">
/**
 * 液态玻璃控制：按钮 + 弹出面板（开关 / 模糊滑杆 / 档位预设）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import { BLUR_MAX, BLUR_MIN, useGlass } from '../../composables/useGlass'

const { enabled, blur, setGlassEnabled, setGlassBlur, applyGlassPreset } = useGlass()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

function onDocPointerDown(e: PointerEvent) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointerDown)
  document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointerDown)
  document.removeEventListener('keydown', onKeydown)
})

const presets = [
  { label: '关', value: 'off' as const },
  { label: '弱', value: 'weak' as const },
  { label: '中', value: 'medium' as const },
  { label: '强', value: 'strong' as const },
]

/** 当前命中哪个档位（用于高亮预设） */
const activePreset = computed(() => {
  if (!enabled.value) return 'off'
  if (blur.value <= 12) return 'weak'
  if (blur.value >= 34) return 'strong'
  return 'medium'
})

function fillPct(): string {
  const span = BLUR_MAX - BLUR_MIN
  return `${((blur.value - BLUR_MIN) / span) * 100}%`
}
</script>

<template>
  <div ref="root" class="gc-root">
    <button
      class="gc-trigger"
      :class="{ 'is-open': open }"
      :aria-expanded="open"
      aria-haspopup="dialog"
      title="液态玻璃"
      aria-label="液态玻璃设置"
      @click="open = !open"
    >
      <Icon name="droplet" :size="15" />
    </button>

    <transition name="gc-pop">
      <div v-if="open" class="gc-panel" role="dialog" aria-label="液态玻璃设置">
        <div class="gc-head">
          <span class="gc-title">液态玻璃</span>
          <button
            class="gc-switch"
            :class="{ 'is-on': enabled }"
            role="switch"
            :aria-checked="enabled"
            aria-label="启用液态玻璃"
            @click="setGlassEnabled(!enabled)"
          >
            <span class="gc-knob" />
          </button>
        </div>

        <div class="gc-body" :class="{ 'is-disabled': !enabled }">
          <div class="gc-row">
            <span class="gc-label">模糊强度</span>
            <span class="gc-value tnum">{{ enabled ? `${blur}px` : '—' }}</span>
          </div>

          <input
            class="gc-range"
            type="range"
            :min="BLUR_MIN"
            :max="BLUR_MAX"
            step="1"
            :value="blur"
            :disabled="!enabled"
            :style="{ '--fill': fillPct() }"
            aria-label="模糊强度"
            @input="setGlassBlur(Number(($event.target as HTMLInputElement).value))"
          />

          <div class="gc-presets">
            <button
              v-for="p in presets"
              :key="p.value"
              class="gc-preset"
              :class="{ 'is-on': activePreset === p.value }"
              @click="applyGlassPreset(p.value)"
            >
              {{ p.label }}
            </button>
          </div>

          <p class="gc-hint">模糊越弱，底色会自动加实，保证文字可读。</p>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.gc-root { position: relative; display: inline-flex; }

/* 触发按钮 */
.gc-trigger {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 9px;
  background: var(--ctl-fill);
  color: var(--txt-2);
  cursor: pointer;
  transition: background 0.16s var(--ease), color 0.16s var(--ease);
}
.gc-trigger:hover { background: var(--ctl-fill-hover); color: var(--txt-1); }
.gc-trigger.is-open { background: var(--accent); color: #fff; }

/* 弹出面板：本身也是玻璃材质 */
.gc-panel {
  position: absolute;
  top: calc(100% + 10px);
  right: 0;
  z-index: 60;
  width: 248px;
  padding: 14px;
  border-radius: 16px;
  background: rgb(var(--glass-rgb) / calc(var(--glass-a) * 0.94));
  backdrop-filter: blur(calc(var(--glass-blur) * 1.4)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(calc(var(--glass-blur) * 1.4)) saturate(var(--glass-sat));
  box-shadow:
    inset 0 1px 0 var(--glass-hi),
    inset 0 0 0 0.5px var(--glass-edge),
    0 18px 44px -18px rgba(0, 0, 0, 0.42);
}

.gc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.gc-title { font-size: 13px; font-weight: 600; letter-spacing: -0.01em; color: var(--txt-1); }

/* Apple 风格开关 */
.gc-switch {
  position: relative;
  width: 40px;
  height: 24px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--ctl-fill-hover);
  cursor: pointer;
  transition: background 0.2s var(--ease);
}
.gc-switch.is-on { background: var(--accent); }
.gc-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 20px;
  height: 20px;
  border-radius: 999px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.28);
  transition: transform 0.2s var(--ease);
}
.gc-switch.is-on .gc-knob { transform: translateX(16px); }

.gc-body { margin-top: 14px; transition: opacity 0.2s var(--ease); }
.gc-body.is-disabled { opacity: 0.38; pointer-events: none; }

.gc-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.gc-label { font-size: 12px; color: var(--txt-2); }
.gc-value { font-size: 12px; font-weight: 600; color: var(--txt-1); }

/* 滑杆 */
.gc-range {
  width: 100%;
  margin: 9px 0 12px;
  appearance: none;
  height: 5px;
  border-radius: 999px;
  cursor: pointer;
  background: linear-gradient(
    to right,
    var(--accent) 0%,
    var(--accent) var(--fill, 50%),
    var(--bar-track) var(--fill, 50%),
    var(--bar-track) 100%
  );
}
.gc-range::-webkit-slider-thumb {
  appearance: none;
  width: 17px;
  height: 17px;
  border-radius: 999px;
  background: #fff;
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
  cursor: grab;
}
.gc-range::-webkit-slider-thumb:active { cursor: grabbing; }
.gc-range::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border-radius: 999px;
  background: #fff;
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
}

/* 档位预设 */
.gc-presets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
.gc-preset {
  padding: 5px 0;
  border-radius: 8px;
  background: var(--ctl-fill);
  font-size: 12px;
  color: var(--txt-2);
  cursor: pointer;
  transition: background 0.16s var(--ease), color 0.16s var(--ease);
}
.gc-preset:hover { background: var(--ctl-fill-hover); color: var(--txt-1); }
.gc-preset.is-on {
  background: var(--accent);
  color: #fff;
  font-weight: 600;
}

.gc-hint {
  margin-top: 11px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--txt-3);
}

/* 弹出动画 */
.gc-pop-enter-active,
.gc-pop-leave-active {
  transition: opacity 0.16s var(--ease), transform 0.16s var(--ease);
  transform-origin: top right;
}
.gc-pop-enter-from,
.gc-pop-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.97);
}
</style>
