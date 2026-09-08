<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  MobileTokenAdminApi,
  type MobileTokenMutationResult,
  type MobileTokenStatus,
  type TokenShareUrls,
} from '../api/endpoints'
import { useToast } from '../composables/useToast'

const toast = useToast()

const loading = ref(false)
const status = ref<MobileTokenStatus | null>(null)
const result = ref<MobileTokenMutationResult | null>(null)
const statusError = ref('')
const actionError = ref('')

const customToken = ref('')
const customBusy = ref(false)
const rotateBusy = ref(false)
const resetBusy = ref(false)

const enabled = computed(() => result.value?.enabled ?? status.value?.enabled ?? false)

const tokenHint = computed(() => {
  if (result.value?.token) return '新 Token 已生成'
  return status.value?.token_hint || '未设置'
})

// 常驻分享链接：优先显示操作后的带 token 链接，否则显示状态查询返回的基础链接。
const currentLinks = computed<TokenShareUrls | null>(() => {
  if (result.value?.share_urls) return result.value.share_urls
  if (status.value?.share_base_urls) return status.value.share_base_urls
  return null
})

const shareLabels: Array<{ key: keyof TokenShareUrls; label: string }> = [
  { key: 'recommended', label: '推荐链接' },
  { key: 'tailscale', label: 'Tailscale 链接（同 tailnet）' },
  { key: 'funnel', label: 'Funnel 公网链接' },
  { key: 'lan', label: '局域网链接' },
]

async function loadStatus() {
  loading.value = true
  statusError.value = ''
  try {
    status.value = await MobileTokenAdminApi.status()
  } catch (e: any) {
    statusError.value = e?.message || String(e) || '加载失败'
  } finally {
    loading.value = false
  }
}

async function rotateToken() {
  rotateBusy.value = true
  actionError.value = ''
  try {
    result.value = await MobileTokenAdminApi.rotate({ length: 32 })
    toast.success('已生成新 Token')
    await loadStatus()
  } catch (e: any) {
    actionError.value = e?.message || String(e) || '生成失败'
    toast.error('生成新 Token 失败')
  } finally {
    rotateBusy.value = false
  }
}

async function setCustomToken() {
  const token = customToken.value.trim()

  if (!token) {
    toast.error('请输入自定义 Token')
    return
  }
  if (/\s/.test(token)) {
    toast.error('Token 不能包含空格')
    return
  }
  if (!/^[A-Za-z0-9_\-.@]{4,64}$/.test(token)) {
    toast.error('Token 只能包含 A-Z a-z 0-9 _ - . @，长度 4-64')
    return
  }

  customBusy.value = true
  actionError.value = ''
  try {
    result.value = await MobileTokenAdminApi.set({ token })
    customToken.value = ''
    toast.success('自定义 Token 已保存')
    await loadStatus()
  } catch (e: any) {
    actionError.value = e?.message || String(e) || '保存失败'
    toast.error('自定义 Token 保存失败')
  } finally {
    customBusy.value = false
  }
}

async function resetToken() {
  const confirmed = window.confirm(
    '确认关闭移动端 Token 鉴权？\n\n如果正在使用 Tailscale Funnel 公网模式，任何互联网用户都可能访问上传接口。'
  )
  if (!confirmed) return

  resetBusy.value = true
  actionError.value = ''
  try {
    result.value = await MobileTokenAdminApi.reset()
    toast.success('Token 鉴权已关闭')
    await loadStatus()
  } catch (e: any) {
    actionError.value = e?.message || String(e) || '重置失败'
    toast.error('重置 Token 失败')
  } finally {
    resetBusy.value = false
  }
}

async function copyText(value?: string | null) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    toast.success('已复制')
  } catch {
    toast.error('复制失败，请手动复制')
  }
}

onMounted(loadStatus)
</script>

<template>
  <div class="mx-auto max-w-4xl px-4 py-6 text-slate-100">
    <header class="mb-6">
      <p class="text-xs font-semibold uppercase tracking-[.18em] text-blue-400">
        v5.1 Control Panel
      </p>
      <h1 class="mt-1 text-2xl font-bold text-white">🔐 远程上传凭证</h1>
      <p class="mt-2 text-sm text-slate-400">
        管理移动端上传 Token。Token 修改后立即生效，无需重启后端。
      </p>
    </header>

    <!-- 状态 -->
    <section class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div class="flex items-center justify-between gap-4">
        <div>
          <h2 class="text-sm font-semibold text-slate-200">当前状态</h2>
          <p class="mt-1 text-sm">
            <span
              class="mr-2 inline-block h-2 w-2 rounded-full"
              :class="enabled ? 'bg-emerald-500' : 'bg-amber-500'"
            ></span>
            {{ enabled ? 'Token 鉴权已启用' : 'Token 鉴权已关闭' }}
          </p>
        </div>

        <button
          class="btn btn-ghost !px-3 !py-1.5 text-xs"
          :disabled="loading"
          @click="loadStatus"
        >
          {{ loading ? '刷新中…' : '刷新状态' }}
        </button>
      </div>

      <div v-if="statusError" class="mt-3 rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-200">
        {{ statusError }}
      </div>

      <div v-else class="mt-3 text-sm text-slate-400">
        当前 Token：<code class="text-slate-200">{{ tokenHint }}</code>
      </div>
    </section>

    <!-- 手机远程上传入口（常驻显示） -->
    <section class="mt-6 rounded-xl border border-emerald-900/60 bg-emerald-950/20 p-4">
      <div class="flex items-center justify-between gap-4">
        <h2 class="text-sm font-semibold text-emerald-200">📱 手机远程上传入口</h2>
        <span class="rounded-full border border-emerald-700 bg-emerald-900/40 px-2 py-0.5 text-[11px] text-emerald-300">
          {{ enabled ? '鉴权已启用' : '无鉴权' }}
        </span>
      </div>

      <template v-if="currentLinks">
        <div class="mt-4 space-y-3">
          <div v-for="{ key, label } in shareLabels" :key="key">
            <p class="mb-1 text-xs text-slate-400">{{ label }}</p>
            <div class="flex items-center gap-2">
              <code class="flex-1 break-all rounded bg-slate-950 px-2 py-2 text-xs text-emerald-300">
                {{ currentLinks[key] || '不可用' }}
              </code>
              <button
                class="btn btn-ghost !px-2 !py-1 text-xs"
                @click="copyText(currentLinks[key])"
              >
                复制
              </button>
            </div>
          </div>
        </div>

        <p class="mt-3 text-xs text-slate-400">
          上方为基础链接。点击「一键生成新 Token」或「保存自定义 Token」后，
          会立即显示出带 <code class="text-amber-300">?token=</code> 的专属链接，复制后发给手机即可。
        </p>
      </template>

      <p v-else-if="statusError" class="mt-3 text-sm text-rose-200">
        无法获取远程地址：{{ statusError }}
      </p>

      <p v-else class="mt-3 text-sm text-amber-300">
        正在获取远程地址…
      </p>
    </section>

    <!-- 操作 -->
    <section class="mt-6 grid gap-4 md:grid-cols-2">
      <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <h2 class="text-sm font-semibold text-slate-200">一键生成新 Token</h2>
        <p class="mt-1 text-xs text-slate-400">
          自动生成随机 Token，旧 Token 立即失效，并产出带 token 的专属链接。
        </p>
        <button
          class="btn btn-primary mt-3 w-full"
          :disabled="rotateBusy"
          @click="rotateToken"
        >
          {{ rotateBusy ? '生成中…' : '一键生成新 Token' }}
        </button>
      </div>

      <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <h2 class="text-sm font-semibold text-slate-200">设置自定义 Token</h2>
        <p class="mt-1 text-xs text-slate-400">
          支持 A-Z a-z 0-9 _ - . @，长度 4-64。
        </p>

        <input
          v-model="customToken"
          class="mt-3 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          placeholder="例如 my-secret-2026"
        />

        <button
          class="btn btn-secondary mt-3 w-full"
          :disabled="customBusy"
          @click="setCustomToken"
        >
          {{ customBusy ? '保存中…' : '保存自定义 Token' }}
        </button>
      </div>
    </section>

    <!-- 重置 -->
    <section class="mt-6 rounded-xl border border-rose-900/60 bg-rose-950/20 p-4">
      <h2 class="text-sm font-semibold text-rose-200">重置 / 关闭 Token</h2>
      <p class="mt-1 text-xs text-rose-200/80">
        关闭后所有请求不再校验 Token。Funnel 公网模式下存在安全风险。
      </p>
      <button
        class="btn mt-3 w-full border border-rose-800 bg-rose-900/40 text-rose-100 hover:bg-rose-900/60"
        :disabled="resetBusy"
        @click="resetToken"
      >
        {{ resetBusy ? '重置中…' : '重置为无鉴权模式' }}
      </button>
    </section>

    <!-- 最新 Token + 专属链接（操作后出现） -->
    <section v-if="result" class="mt-6 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <h2 class="text-sm font-semibold text-slate-200">最新 Token</h2>

      <div v-if="result.token" class="mt-3 rounded-lg border border-slate-700 bg-slate-950 p-3">
        <p class="text-xs text-slate-400">新 Token（仅本次显示）</p>
        <div class="mt-1 flex items-center gap-2">
          <code class="flex-1 break-all text-emerald-300">{{ result.token }}</code>
          <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="copyText(result.token)">
            复制
          </button>
        </div>
      </div>

      <p class="mt-3 text-xs text-amber-300">
        上面「手机远程上传入口」中的链接已自动更新为带新 token 的完整链接。请使用新链接重新打开手机端页面，否则手机端本地保存的旧 Token 不会自动更新。
      </p>
    </section>

    <p v-if="actionError" class="mt-4 rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-200">
      {{ actionError }}
    </p>

    <section class="mt-6 rounded-xl border border-slate-800 bg-slate-900/40 p-4 text-xs text-slate-400">
      <p>· 管理 API 仅允许电脑本机访问。</p>
      <p>· 手机端、Tailscale 远程端、Funnel 公网端都无法修改 Token。</p>
      <p>· 状态查询不会显示完整 Token。忘记 Token 时直接重新生成即可。</p>
    </section>
  </div>
</template>
