<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ModelsApi, RoutesApi, UsageApi } from '../api/endpoints'
import type { ModelItem, RoutesInfo } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import ProgressBar from './widgets/ProgressBar.vue'
import { parseUsageWindows, statusLabel } from '../utils/format'

const models = ref<ModelItem[]>([])
const usage = ref<any>(null)
const usageError = ref('')
const modelError = ref('')
const routesInfo = ref<RoutesInfo | null>(null)
const routeError = ref('')
const workflow = ref<'lesson' | 'homework' | 'error' | 'review'>('lesson')

async function loadModels() {
  try {
    models.value = await ModelsApi.list()
  } catch (e: any) {
    modelError.value = e?.message || String(e)
  }
}

async function loadUsage() {
  usageError.value = ''
  try {
    usage.value = await UsageApi.get()
  } catch (e: any) {
    usage.value = null
    usageError.value = e?.message || String(e)
  }
}

async function loadRoutes() {
  routeError.value = ''
  try {
    routesInfo.value = await RoutesApi.get(workflow.value)
  } catch (e: any) {
    routesInfo.value = null
    routeError.value = e?.message || String(e)
  }
}

onMounted(async () => {
  await Promise.all([loadModels(), loadUsage()])
  await loadRoutes()
})

const quota = computed(() => {
  const parsed = parseUsageWindows(usage.value)
  const wins = parsed.windows
  const w5 = wins[0] || { used: 0, limit: 0, pct: null }
  const w7 = wins[1] || { used: 0, limit: 0, pct: null }
  const w30 = wins[2] || { used: 0, limit: 0, pct: null }
  return {
    available: parsed.available,
    pct5: w5.pct,
    pct7: w7.pct,
    pct30: w30.pct,
    used5: w5.used, limit5: w5.limit,
    used7: w7.used, limit7: w7.limit,
    used30: w30.used, limit30: w30.limit,
    provider: parsed.provider,
  }
})

const groupedModels = computed(() => {
  const groups: Record<string, ModelItem[]> = {}
  models.value.forEach(m => {
    const key = m.family || 'other'
    if (!groups[key]) groups[key] = []
    groups[key].push(m)
  })
  return groups
})

const FAMILY_LABEL: Record<string, string> = {
  deepseek: 'DeepSeek',
  qwen: 'Qwen',
  embedding: 'Embedding / Rerank',
  rerank: 'Embedding / Rerank',
  minimax: 'MiniMax',
  glm: 'GLM',
  other: '其他',
}

const ROLE_LABEL: Record<string, string> = {
  vision_reader: '视觉识别',
  transcriber_splitter: '转写切段',
  lesson_structurer: '课堂结构',
  student_simulator: '学生视角模拟',
  note_writer: '笔记写作',
  critic: '独立审查',
  evidence_auditor: '证据审查',
  scope_auditor: '范围审查',
  solver: '解题主链',
  parallel_solver: '高风险并行',
  solution_explainer: '解题解释',
  error_analyst: '错因分析',
  review_writer: '复习写作',
  self_test_writer: '自测卷生成',
}

function modelLabel(id: string): string {
  const m = models.value.find(x => x.id === id)
  if (!m) return id
  return `${m.gateway_model}`
}

function modelProvider(id: string): string {
  const m = models.value.find(x => x.id === id)
  return m ? m.provider : 'unknown'
}

const HIGHLIGHT_ROLES = ['solver', 'parallel_solver', 'critic', 'vision_reader', 'evidence_auditor', 'solution_explainer']

function highlight(role: string): boolean {
  return HIGHLIGHT_ROLES.includes(role)
}

const routeRoles = computed(() => {
  if (!routesInfo.value) return [] as Array<{ role: string; model: string }>
  return Object.entries(routesInfo.value.routes).map(([role, model]) => ({ role, model }))
})
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="🤖"
      title="模型路由与额度"
      subtitle="展示模型注册表、按工作流的路由、微信免费 DeepSeek 多窗口额度。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="loadUsage">刷新额度</button>
      </template>
    </PageHeader>

    <!-- 额度 -->
    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">微信免费 DeepSeek 额度</h3>
          <p class="card-muted">来自 8080 网关；阈值 ≥70% 进入保守模式，≥90% 全部切官方付费。</p>
        </div>
        <StatusBadge :status="quota.available ? 'available' : 'unavailable'"
                     :variant="quota.available ? 'green' : 'amber'">
          {{ quota.available ? '已连接' : '未连接' }}
        </StatusBadge>
      </div>
      <States :error="usageError" :loading="!quota.available && !usageError">
        <div class="grid gap-3 md:grid-cols-3">
          <div class="card-soft">
            <p class="text-xs text-slate-400">5 小时</p>
            <p class="mt-1 text-2xl font-bold text-slate-100">
              {{ quota.used5 }} / {{ quota.limit5 }}
            </p>
            <ProgressBar :percent="quota.pct5" class="mt-2" />
          </div>
          <div class="card-soft">
            <p class="text-xs text-slate-400">7 天</p>
            <p class="mt-1 text-2xl font-bold text-slate-100">
              {{ quota.used7 }} / {{ quota.limit7 }}
            </p>
            <ProgressBar :percent="quota.pct7" :warn-at="80" class="mt-2" />
          </div>
          <div class="card-soft">
            <p class="text-xs text-slate-400">30 天</p>
            <p class="mt-1 text-2xl font-bold text-slate-100">
              {{ quota.used30 }} / {{ quota.limit30 }}
            </p>
            <ProgressBar :percent="quota.pct30" :warn-at="80" class="mt-2" />
          </div>
        </div>
      </States>
    </section>

    <!-- 路由 -->
    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">按工作流的路由</h3>
          <p class="card-muted">当前 quota_pct_5h：{{ routesInfo?.quota_pct_5h?.toFixed(1) ?? '—' }}%</p>
        </div>
        <select v-model="workflow" class="input !w-48" @change="loadRoutes">
          <option value="lesson">听课流</option>
          <option value="homework">作业流</option>
          <option value="error">错题流</option>
          <option value="review">复习流</option>
        </select>
      </div>
      <States :loading="!routesInfo && !routeError" :error="routeError">
        <div class="overflow-x-auto rounded-xl border border-slate-800">
          <table class="data-table bg-slate-950/40">
            <thead>
              <tr>
                <th>角色</th>
                <th>首选模型</th>
                <th>Provider</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in routeRoles" :key="r.role"
                  :class="highlight(r.role) ? 'bg-blue-950/30' : ''">
                <td>
                  {{ ROLE_LABEL[r.role] || r.role }}
                  <span class="ml-1 text-[10px] text-slate-500">({{ r.role }})</span>
                </td>
                <td class="font-mono text-slate-200">{{ modelLabel(r.model) }}</td>
                <td>
                  <StatusBadge :status="modelProvider(r.model)" variant="blue">
                    {{ statusLabel(modelProvider(r.model)) }}
                  </StatusBadge>
                </td>
                <td class="text-xs text-slate-400">
                  <template v-if="r.role === 'solver'">主链路解题</template>
                  <template v-else-if="r.role === 'parallel_solver'">高风险题并行</template>
                  <template v-else-if="r.role === 'critic'">独立审查</template>
                  <template v-else-if="r.role === 'vision_reader'">Qwen3-VL 视觉</template>
                  <template v-else-if="r.role === 'evidence_auditor'">证据链审查</template>
                  <template v-else-if="r.role === 'solution_explainer'">解题解释</template>
                  <template v-else>—</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </States>
    </section>

    <!-- 模型注册表 -->
    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">模型注册表</h3>
          <p class="card-muted">所有路由候选模型；默认角色用徽标标识。</p>
        </div>
      </div>
      <States :empty="!models.length" empty-icon="🤖" empty-title="模型注册表为空" :error="modelError">
        <div v-for="(items, family) in groupedModels" :key="family" class="mb-4">
          <p class="mb-2 text-xs font-semibold uppercase tracking-[.14em] text-slate-500">
            {{ FAMILY_LABEL[family] || family }}
          </p>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <article v-for="m in items" :key="m.id" class="card-soft">
              <div class="flex items-start justify-between gap-2">
                <p class="truncate font-mono text-sm font-semibold text-slate-100">{{ m.gateway_model }}</p>
                <span class="chip">{{ m.capability }}</span>
              </div>
              <p class="mt-1 text-[11px] text-slate-500">id: {{ m.id }} · provider: {{ m.provider }}</p>
              <div class="mt-2 flex flex-wrap gap-1">
                <StatusBadge v-if="m.is_default_solver" status="default_solver" variant="green">主链</StatusBadge>
                <StatusBadge v-if="m.is_default_reviewer" status="default_reviewer" variant="blue">审查</StatusBadge>
                <StatusBadge v-if="m.is_default_vision" status="default_vision" variant="purple">视觉</StatusBadge>
                <StatusBadge v-if="m.is_default_light" status="default_light" variant="amber">轻量</StatusBadge>
              </div>
            </article>
          </div>
        </div>
      </States>
    </section>
  </div>
</template>
