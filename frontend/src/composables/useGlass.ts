/**
 * 液态玻璃材质控制（开关 + 模糊强度）。
 *
 * - 单例：模块级状态，所有组件共享。
 * - 持久化到 localStorage，键名 v5-glass。
 * - 落点：
 *   <html data-glass="on|off">                 —— 开关
 *   <html style="--glass-blur: Npx">           —— 模糊半径
 *   <html style="--glass-boost: 0..1">         —— 模糊越弱、底色越实（保证可读性）
 *
 * --glass-boost 的用途：模糊降到 0 时，纯半透明面板会露出壁纸的硬边色块，
 * 文字也压不住。所以模糊越小，越往上加底色不透明度。
 */
import { ref } from 'vue'

export type GlassPreset = 'off' | 'weak' | 'medium' | 'strong'

const STORAGE_KEY = 'v5-glass'
export const BLUR_MIN = 0
export const BLUR_MAX = 40
export const BLUR_DEFAULT = 22

const enabled = ref(true)
const blur = ref(BLUR_DEFAULT)

function clampBlur(v: unknown): number {
  const n = Number(v)
  if (!Number.isFinite(n)) return BLUR_DEFAULT
  return Math.min(BLUR_MAX, Math.max(BLUR_MIN, Math.round(n)))
}

function apply() {
  if (typeof document === 'undefined') return
  const el = document.documentElement
  el.setAttribute('data-glass', enabled.value ? 'on' : 'off')
  const px = enabled.value ? blur.value : 0
  el.style.setProperty('--glass-blur', `${px}px`)
  // 模糊 40px -> boost 0（最实）；模糊 0px -> boost 1（最不透）
  el.style.setProperty('--glass-boost', String(Math.max(0, 1 - px / BLUR_MAX)))
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: enabled.value, blur: blur.value }))
  } catch {
    /* ignore */
  }
}

let booted = false

/** 初始化一次即可，重复调用无副作用。 */
export function initGlass() {
  if (booted) return
  booted = true
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (typeof parsed?.enabled === 'boolean') enabled.value = parsed.enabled
      if (parsed?.blur !== undefined) blur.value = clampBlur(parsed.blur)
    }
  } catch {
    /* ignore */
  }
  apply()
}

export function setGlassEnabled(next: boolean) {
  enabled.value = next
  apply()
  persist()
}

export function setGlassBlur(next: number) {
  blur.value = clampBlur(next)
  // 从 0 拉起来时顺手打开，符合直觉
  if (blur.value > 0) enabled.value = true
  apply()
  persist()
}

export function applyGlassPreset(preset: GlassPreset) {
  switch (preset) {
    case 'off':
      enabled.value = false
      break
    case 'weak':
      enabled.value = true
      blur.value = 8
      break
    case 'medium':
      enabled.value = true
      blur.value = 22
      break
    case 'strong':
      enabled.value = true
      blur.value = 40
      break
  }
  apply()
  persist()
}

export function useGlass() {
  initGlass()
  return { enabled, blur, setGlassEnabled, setGlassBlur, applyGlassPreset }
}
