/**
 * 后端健康状态 store。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { HealthApi } from '../api/endpoints'

export const useHealthStore = defineStore('health', () => {
  const online = ref(false)
  const checking = ref(false)
  const version = ref<string>('')
  const lastError = ref<string>('')

  async function check() {
    checking.value = true
    lastError.value = ''
    try {
      const r = await HealthApi.get()
      online.value = r.status === 'ok'
      version.value = r.version || ''
    } catch (e: any) {
      online.value = false
      lastError.value = e?.message || String(e)
    } finally {
      checking.value = false
    }
  }

  return { online, checking, version, lastError, check }
})
