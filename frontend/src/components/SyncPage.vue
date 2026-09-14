<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { SyncApi } from '../api/endpoints'
import type { SyncJob } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import Icon from './widgets/Icon.vue'
import { fmtRelative, statusLabel } from '../utils/format'
import { useToast } from '../composables/useToast'

const vault = ref<{ vault_root?: string } | null>(null)
const jobs = ref<SyncJob[]>([])
const loading = ref(false)
const error = ref('')
const toast = useToast()

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [v, j] = await Promise.all([SyncApi.vault(), SyncApi.status()])
    vault.value = v
    jobs.value = j || []
  } catch (e: any) {
    error.value = e?.message || String(e)
    toast.error(`同步状态加载失败:${e?.message || e}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const stats = computed(() => {
  const map: Record<string, number> = {}
  jobs.value.forEach(j => {
    const s = (j.status || 'unknown').toLowerCase()
    map[s] = (map[s] || 0) + 1
  })
  return map
})
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="refresh"
      title="Obsidian 同步"
      subtitle="监听听课 / 复习 / 错题等 DAG 输出写入到本地 Obsidian Vault 的同步状态。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="load">
          <Icon name="refresh" :size="15" />
          刷新
        </button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error">
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">Vault 路径</h3>
            <p class="card-muted">由后端配置 OBSIDIAN_VAULT_ROOT 控制。</p>
          </div>
        </div>
        <div class="card-soft flex items-center gap-3">
          <span class="grid h-9 w-9 shrink-0 place-items-center rounded-[10px] border border-[var(--accent-line)] bg-[var(--accent-soft)] text-[var(--accent-hi)]">
            <Icon name="server" :size="17" />
          </span>
          <div class="min-w-0">
            <p class="truncate font-mono text-[13px] text-slate-100">
              {{ vault?.vault_root || '未配置 Vault 路径' }}
            </p>
            <p class="mt-0.5 text-[11.5px] text-t3">用于听课笔记 / 复习笔记 / 错题本自动落盘</p>
          </div>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">同步任务</h3>
            <p class="card-muted">展示最近写入 Vault 的资产。</p>
          </div>
          <span class="chip">{{ jobs.length }} 条</span>
        </div>

        <div class="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
          <div v-for="(c, s) in stats" :key="s" class="card-soft !p-3 text-center">
            <p class="text-xs text-slate-400">{{ s }}</p>
            <p class="text-xl font-bold text-slate-100">{{ c }}</p>
          </div>
        </div>

        <States :empty="!jobs.length" empty-icon="refresh" empty-title="暂无同步任务"
                empty-hint="运行听课 / 复习 / 错题 DAG 后会自动写入 Vault。">
          <div class="overflow-x-auto rounded-xl border border-slate-800">
            <table class="data-table bg-slate-950/40">
              <thead>
                <tr>
                  <th>资产</th>
                  <th>目标</th>
                  <th>状态</th>
                  <th>重试</th>
                  <th>同步时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="j in jobs" :key="j.id">
                  <td>
                    <p class="font-medium text-slate-100">{{ j.asset_path || `任务 #${j.id}` }}</p>
                    <p class="text-[11px] text-slate-500">#{{ j.id }} · {{ j.content_hash || '—' }}</p>
                  </td>
                  <td class="font-mono text-xs text-slate-300">{{ j.target || '—' }}</td>
                  <td>
                    <StatusBadge :status="j.status">{{ statusLabel(j.status) }}</StatusBadge>
                    <p v-if="j.last_error" class="mt-1 text-[11px] text-[var(--acc-red)]">{{ j.last_error }}</p>
                  </td>
                  <td class="text-xs text-slate-400">{{ j.retries ?? 0 }}</td>
                  <td class="text-xs text-slate-400">{{ fmtRelative(j.synced_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </States>
      </section>
    </States>
  </div>
</template>
