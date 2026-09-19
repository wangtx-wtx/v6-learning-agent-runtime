<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ModelsApi, UsageApi } from '../api/endpoints'
import type { GatewayServicesStatus, ModelItem } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import ProgressBar from './widgets/ProgressBar.vue'
import { parseUsageWindows } from '../utils/format'

const models = ref<ModelItem[]>([])
const roleRoutes = ref<Record<string, string[]>>({})
const usage = ref<any>(null)
const gatewayServices = ref<GatewayServicesStatus>({
  available: false,
  models: [],
  detail: '',
  admin_url: 'http://127.0.0.1:8317/admin/specialists.html',
})
const loading = ref(true)
const error = ref('')
const notice = ref('')
const editing = ref(false)
const originalId = ref('')

const blankModel = (): ModelItem => ({
  id: '', gateway_model: '', provider: '', family: 'other', capability: 'text',
  enabled: true, is_builtin: false, context_window: null, embedding_dimensions: null,
  input_cost: null, output_cost: null, cost_unit: 'CNY/1M tokens', notes: '',
  is_default_solver: false, is_default_reviewer: false,
  is_default_vision: false, is_default_light: false,
})
const form = ref<ModelItem>(blankModel())

const ROLE_LABEL: Record<string, string> = {
  solver: '主力解题', parallel_solver: '高风险并行', adjudicator: '争议仲裁',
  critic: '独立审查', evidence_auditor: '证据核验', scope_auditor: '范围审查',
  vision_reader: '图片理解', ocr: '文字识别 OCR', embedding: '向量检索', rerank: '检索重排',
  transcriber_splitter: '转写切段', lesson_structurer: '课堂结构',
  student_simulator: '学生视角', note_writer: '笔记写作',
  solution_explainer: '解题讲解', error_analyst: '错因分析',
  review_writer: '复习写作', self_test_writer: '自测题生成',
  // V6 Phase 2：全量分段理解（每段一次调用，需长上下文）
  segment_understanding: '分段理解',
  // V6 Phase 2 增强：分段边界规划（读懂全文后标出知识点边界与切点）
  segment_boundary_planner: '分段边界规划',
}
const CAPABILITY_LABEL: Record<string, string> = {
  text: '文本', vision: '视觉', multimodal: '多模态', ocr: 'OCR',
  embedding: '向量', rerank: '重排',
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [modelData, routeData, usageData, serviceData] = await Promise.all([
      ModelsApi.list(), ModelsApi.routes(), UsageApi.get().catch(() => null),
      ModelsApi.gatewayServices().catch((e: any) => ({
        available: false,
        models: [],
        detail: e?.message || String(e),
        admin_url: 'http://127.0.0.1:8317/admin/specialists.html',
      })),
    ])
    models.value = modelData
    roleRoutes.value = routeData
    usage.value = usageData
    gatewayServices.value = serviceData
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

const enabledModels = computed(() => models.value.filter(m => m.enabled))
function modelsForRole(role: string): ModelItem[] {
  if (role === 'embedding') return enabledModels.value.filter(m => m.capability === 'embedding')
  if (role === 'rerank') return enabledModels.value.filter(m => m.capability === 'rerank')
  if (role === 'ocr') return enabledModels.value.filter(m => ['ocr', 'vision', 'multimodal'].includes(m.capability))
  if (role === 'vision_reader') return enabledModels.value.filter(m => ['vision', 'multimodal'].includes(m.capability))
  return enabledModels.value.filter(m => ['text', 'vision', 'multimodal'].includes(m.capability))
}
const groupedModels = computed(() => {
  const groups: Record<string, ModelItem[]> = {}
  for (const model of models.value) {
    const key = model.family || 'other'
    ;(groups[key] ||= []).push(model)
  }
  return groups
})

const quota = computed(() => {
  const parsed = parseUsageWindows(usage.value)
  const windows = parsed.windows
  return {
    available: parsed.available,
    items: [0, 1, 2].map(i => windows[i] || { used: 0, limit: 0, pct: null }),
    provider: parsed.provider,
  }
})

function openNew() {
  form.value = blankModel()
  originalId.value = ''
  editing.value = true
  notice.value = ''
}

function openEdit(model: ModelItem) {
  form.value = { ...model }
  originalId.value = model.id
  editing.value = true
  notice.value = ''
}

async function saveModel() {
  error.value = ''
  notice.value = ''
  if (originalId.value && originalId.value !== form.value.id) {
    error.value = '已有模型的内部 ID 不可修改；可以修改网关模型名。'
    return
  }
  try {
    await ModelsApi.save(form.value)
    notice.value = `模型 ${form.value.gateway_model} 已保存`
    editing.value = false
    await loadAll()
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

async function toggleModel(model: ModelItem) {
  error.value = ''
  try {
    await ModelsApi.setEnabled(model.id, !model.enabled)
    notice.value = `${model.gateway_model} 已${model.enabled ? '停用' : '启用'}`
    await loadAll()
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

function routeAt(role: string, position: number): string {
  return roleRoutes.value[role]?.[position] || ''
}

function setRouteAt(role: string, position: number, value: string) {
  const next = [...(roleRoutes.value[role] || [])]
  while (next.length <= position) next.push('')
  next[position] = value
  roleRoutes.value[role] = next
}

function onRouteChange(role: string, position: number, event: Event) {
  setRouteAt(role, position, (event.target as HTMLSelectElement).value)
}

async function saveRoute(role: string) {
  error.value = ''
  const ids = (roleRoutes.value[role] || []).filter(Boolean)
  try {
    await ModelsApi.saveRoute(role, ids)
    notice.value = `${ROLE_LABEL[role] || role} 的主备路由已生效`
    await loadAll()
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

function price(model: ModelItem) {
  if (model.input_cost == null && model.output_cost == null) return '未填写价格'
  const input = model.input_cost == null ? '—' : model.input_cost
  const output = model.output_cost == null ? '—' : model.output_cost
  return `输入 ${input} / 输出 ${output} · ${model.cost_unit}`
}
</script>

<template>
  <div class="page-shell">
    <PageHeader icon="bot" title="模型中心" subtitle="管理调用模型、能力、价格，以及每个任务的主模型和备用模型。">
      <template #actions>
        <a class="btn btn-secondary" href="http://127.0.0.1:8317/admin" target="_blank">打开网关后台</a>
        <a class="btn btn-secondary" :href="gatewayServices.admin_url" target="_blank">管理向量 / OCR</a>
        <button class="btn btn-secondary" @click="loadAll">刷新</button>
        <button class="btn btn-primary" @click="openNew">新增模型</button>
      </template>
    </PageHeader>

    <p v-if="notice" class="mb-4 rounded-xl border border-emerald-800 bg-emerald-950/40 px-4 py-3 text-sm text-[var(--acc-green)]">{{ notice }}</p>
    <p v-if="error" class="mb-4 rounded-xl border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-[var(--acc-red)]">{{ error }}</p>

    <section v-if="editing" class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">{{ originalId ? '编辑模型' : '新增模型' }}</h3>
          <p class="card-muted">这里只保存模型元数据；API Key 在网关后台维护。</p>
        </div>
        <button class="btn btn-secondary" @click="editing = false">取消</button>
      </div>
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <label class="text-sm text-slate-300">内部 ID
          <input v-model.trim="form.id" class="input mt-1" :disabled="!!originalId" placeholder="例如 glm_flash" />
        </label>
        <label class="text-sm text-slate-300">网关模型名
          <input v-model.trim="form.gateway_model" class="input mt-1" placeholder="例如 glm-5.3-flash" />
        </label>
        <label class="text-sm text-slate-300">供应商
          <input v-model.trim="form.provider" class="input mt-1" placeholder="例如 zhipu" />
        </label>
        <label class="text-sm text-slate-300">模型家族
          <input v-model.trim="form.family" class="input mt-1" placeholder="例如 glm" />
        </label>
        <label class="text-sm text-slate-300">能力
          <select v-model="form.capability" class="input mt-1">
            <option v-for="(label, key) in CAPABILITY_LABEL" :key="key" :value="key">{{ label }}</option>
          </select>
        </label>
        <label class="text-sm text-slate-300">上下文长度
          <input v-model.number="form.context_window" class="input mt-1" type="number" min="1" placeholder="例如 1000000" />
        </label>
        <label v-if="form.capability === 'embedding'" class="text-sm text-slate-300">向量维度
          <input v-model.number="form.embedding_dimensions" class="input mt-1" type="number" min="1" placeholder="例如 1024" />
        </label>
        <label class="text-sm text-slate-300">输入价格
          <input v-model.number="form.input_cost" class="input mt-1" type="number" min="0" step="0.001" />
        </label>
        <label class="text-sm text-slate-300">输出价格
          <input v-model.number="form.output_cost" class="input mt-1" type="number" min="0" step="0.001" />
        </label>
        <label class="text-sm text-slate-300">价格单位
          <input v-model.trim="form.cost_unit" class="input mt-1" placeholder="CNY/1M tokens" />
        </label>
        <label class="text-sm text-slate-300 md:col-span-2">备注
          <input v-model.trim="form.notes" class="input mt-1" placeholder="适合的任务、限制等" />
        </label>
      </div>
      <div class="mt-4 flex justify-end">
        <button class="btn btn-primary" @click="saveModel">保存模型</button>
      </div>
    </section>

    <States :loading="loading" :error="loading ? '' : ''">
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">检索与文档专用模型</h3>
            <p class="card-muted">这些模型通过网关原生专用接口调用，不伪装为聊天模型；密钥只保存在网关。</p>
          </div>
          <StatusBadge :status="gatewayServices.available ? 'enabled' : 'disabled'" :variant="gatewayServices.available ? 'green' : 'amber'">
            {{ gatewayServices.available ? '网关已连接' : '网关不可用' }}
          </StatusBadge>
        </div>
        <p v-if="!gatewayServices.available" class="mb-3 rounded-xl border border-amber-800 bg-amber-950/30 px-4 py-3 text-xs text-amber-200">
          {{ gatewayServices.detail || '暂时无法读取专用模型状态。' }}
        </p>
        <div class="grid gap-3 md:grid-cols-3">
          <article v-for="service in gatewayServices.models" :key="service.id" class="card-soft">
            <div class="flex items-start justify-between gap-2">
              <div>
                <p class="font-mono text-sm font-semibold text-slate-100">{{ service.clientModelId }}</p>
                <p class="mt-1 text-[11px] text-slate-500">{{ service.providerName }} · {{ service.kind }}</p>
              </div>
              <StatusBadge :status="service.enabled ? 'enabled' : 'disabled'" :variant="service.enabled ? 'green' : 'amber'">
                {{ service.enabled ? '启用' : '停用' }}
              </StatusBadge>
            </div>
            <div class="mt-3 flex flex-wrap gap-1">
              <span class="chip">{{ CAPABILITY_LABEL[service.kind] || service.kind }}</span>
              <span v-if="service.dimensions" class="chip">{{ service.dimensions }} 维</span>
              <span v-if="service.maxInputTokens" class="chip">{{ service.maxInputTokens.toLocaleString() }} Token</span>
            </div>
            <p class="mt-3 truncate text-[11px] text-slate-500" :title="service.upstreamPath">{{ service.upstreamPath }}</p>
          </article>
          <article v-if="gatewayServices.available && !gatewayServices.models.length" class="card-soft md:col-span-3">
            <p class="text-sm text-slate-300">网关已连接，但尚未注册专用模型。点击“管理向量 / OCR”完成配置。</p>
          </article>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">任务调用路由</h3>
            <p class="card-muted">首选失败后会按备用 1、备用 2 的顺序切换。</p>
          </div>
        </div>
        <div class="overflow-x-auto rounded-xl border border-slate-800">
          <table class="data-table bg-slate-950/40">
            <thead><tr><th>任务角色</th><th>首选模型</th><th>备用 1</th><th>备用 2</th><th></th></tr></thead>
            <tbody>
              <tr v-for="(_, role) in roleRoutes" :key="role">
                <td><span class="font-medium text-slate-200">{{ ROLE_LABEL[role] || role }}</span><br><span class="text-[10px] text-slate-500">{{ role }}</span></td>
                <td v-for="position in [0, 1, 2]" :key="position">
                  <select class="input min-w-44" :value="routeAt(role, position)" @change="onRouteChange(role, position, $event)">
                    <option v-if="position > 0" value="">不设置</option>
                    <option v-for="m in modelsForRole(String(role))" :key="m.id" :value="m.id">{{ m.gateway_model }}</option>
                  </select>
                </td>
                <td><button class="btn btn-secondary" @click="saveRoute(role)">应用</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <div><h3 class="card-title">模型注册表</h3><p class="card-muted">停用模型前，需要先把它从对应任务的首选位置移走。</p></div>
        </div>
        <div v-for="(items, family) in groupedModels" :key="family" class="mb-5">
          <p class="mb-2 text-xs font-semibold uppercase tracking-[.14em] text-slate-500">{{ family }}</p>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <article v-for="m in items" :key="m.id" class="card-soft" :class="!m.enabled ? 'opacity-50' : ''">
              <div class="flex items-start justify-between gap-2">
                <div><p class="font-mono text-sm font-semibold text-slate-100">{{ m.gateway_model }}</p><p class="mt-1 text-[11px] text-slate-500">{{ m.id }} · {{ m.provider }}</p></div>
                <StatusBadge :status="m.enabled ? 'enabled' : 'disabled'" :variant="m.enabled ? 'green' : 'amber'">{{ m.enabled ? '启用' : '停用' }}</StatusBadge>
              </div>
              <div class="mt-3 flex flex-wrap gap-1"><span class="chip">{{ CAPABILITY_LABEL[m.capability] || m.capability }}</span><span v-if="m.embedding_dimensions" class="chip">{{ m.embedding_dimensions }} 维</span><span v-if="m.context_window" class="chip">{{ m.context_window.toLocaleString() }} 上下文</span></div>
              <p class="mt-3 text-xs text-slate-400">{{ price(m) }}</p>
              <p v-if="m.notes" class="mt-2 text-xs text-slate-500">{{ m.notes }}</p>
              <div class="mt-3 flex gap-2"><button class="btn btn-secondary" @click="openEdit(m)">编辑</button><button class="btn btn-secondary" @click="toggleModel(m)">{{ m.enabled ? '停用' : '启用' }}</button></div>
            </article>
          </div>
        </div>
      </section>

      <section class="section">
        <div class="section-head"><div><h3 class="card-title">网关额度状态</h3><p class="card-muted">{{ quota.provider || '当前网关暂未提供统一额度接口' }}</p></div></div>
        <div class="grid gap-3 md:grid-cols-3">
          <div v-for="(item, i) in quota.items" :key="i" class="card-soft"><p class="text-xs text-slate-400">{{ ['5 小时', '7 天', '30 天'][i] }}</p><p class="mt-1 text-lg font-bold text-slate-100">{{ item.used }} / {{ item.limit }}</p><ProgressBar :percent="item.pct" class="mt-2" /></div>
        </div>
      </section>
    </States>
  </div>
</template>
