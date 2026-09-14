/**
 * 主题控制（浅色 / 深色 / 跟随系统）。
 *
 * - 单例：模块级状态，任何组件引入即共享同一份。
 * - 持久化到 localStorage，键名 v5-theme。
 * - 实际主题写在 <html data-theme="light|dark"> 上，由 style.css 读取。
 * - 首屏防闪：index.html 内联脚本会在 CSS 之前先落一次属性。
 */
import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'v5-theme'

export type AppStyle = 'apple' | 'swiss' | 'broadsheet'

function readStored(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    /* 隐私模式等场景忽略 */
  }
  return 'system'
}

/** 风格档：apple（默认，含玻璃材质）/ swiss（瑞士网格，纯平面） */
function readStoredStyle(): AppStyle {
  try {
    const v = localStorage.getItem(`${STORAGE_KEY}-style`)
    if (v === 'apple' || v === 'swiss' || v === 'broadsheet') return v
  } catch {
    /* ignore */
  }
  return 'apple'
}

function applyStyleToDom(s: AppStyle) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-style', s)
}

const style = ref<AppStyle>(readStoredStyle())

function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return true
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

const mode = ref<ThemeMode>('system')
const systemDark = ref(true)

/** 当前实际生效的主题 */
const resolved = computed<ResolvedTheme>(() =>
  mode.value === 'system' ? (systemDark.value ? 'dark' : 'light') : mode.value,
)

function applyToDom(theme: ResolvedTheme) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-theme', theme)
}

let booted = false

/** 初始化一次即可，重复调用无副作用。 */
export function initTheme() {
  if (booted) return
  booted = true

  mode.value = readStored()
  systemDark.value = systemPrefersDark()

  if (typeof window !== 'undefined' && window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => {
      systemDark.value = e.matches
      if (mode.value === 'system') applyToDom(e.matches ? 'dark' : 'light')
    }
    mq.addEventListener?.('change', onChange)
  }

  applyToDom(resolved.value)
  applyStyleToDom(style.value)
}

export function setThemeMode(next: ThemeMode) {
  mode.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* ignore */
  }
  applyToDom(next === 'system' ? (systemDark.value ? 'dark' : 'light') : next)
}

export function setAppStyle(next: AppStyle) {
  style.value = next
  try {
    localStorage.setItem(`${STORAGE_KEY}-style`, next)
  } catch {
    /* ignore */
  }
  applyStyleToDom(next)
}

/** 在浅色 / 深色之间直接切换（跟随时以当前系统值为起点） */
export function toggleTheme() {
  setThemeMode(resolved.value === 'dark' ? 'light' : 'dark')
}

export function useTheme() {
  initTheme()
  return { mode, resolved, style, setThemeMode, setAppStyle, toggleTheme }
}
