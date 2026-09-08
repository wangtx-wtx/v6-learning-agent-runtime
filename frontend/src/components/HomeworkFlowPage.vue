<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ChaptersApi, CoursesApi, HomeworkWorkflowApi, LessonsApi } from '../api/endpoints'
import type { Chapter, Course, Lesson, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import { toDagNodes } from './widgets/dag-types'
import { prettyJson, safeJson } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])

const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const lessonId = ref<number | ''>('')
const homeworkText = ref('')

const running = ref(false)
const error = ref('')
const runDetail = ref<WorkflowRun | null>(null)
const nodes = ref<ReturnType<typeof toDagNodes>>([])
const outputs = ref<Record<string, any> | null>(null)
const pollTimer = ref<number | null>(null)
const toast = useToast()

async function loadMeta() {
  try {
    const [cs, chs, ls] = await Promise.all([CoursesApi.list(), ChaptersApi.list(), LessonsApi.list()])
    courses.value = cs
    chapters.value = chs
    lessons.value = ls
    if (!courseId.value && cs.length) courseId.value = cs[0].id
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

const filteredChapters = computed(() =>
  courseId.value ? chapters.value.filter(c => c.course_id === Number(courseId.value)) : [],
)
const filteredLessons = computed(() => {
  if (chapterId.value) return lessons.value.filter(l => l.chapter_id === Number(chapterId.value))
  if (courseId.value) return lessons.value.filter(l => l.course_id === Number(courseId.value))
  return []
})

interface RiskItem { index: number; text: string; risk: string; reason: string[] }
interface SolveItem {
  question_no: number | string
  final_answer?: string
  solution_plan?: string
  detailed_solution?: string
  confidence?: number
  model_used?: string
  risk?: { risk?: string; reason?: string[] }
}

const riskItems = computed<RiskItem[]>(() => {
  const node = outputs.value?.risk_classifier || outputs.value?.['risk_classifier']
  return node?.risk_items || []
})

const solves = computed<SolveItem[]>(() => {
  const node = outputs.value?.solver || outputs.value?.['solver']
  return node?.results || node?.solves || []
})

const parallelSolves = computed<any[]>(() => {
  const node = outputs.value?.parallel_solver || outputs.value?.['parallel_solver']
  return node?.results || []
})

const provisionalErrors = computed(() => {
  const auditor = outputs.value?.evidence_auditor || outputs.value?.['evidence_auditor']
  return auditor?.provisional_errors || auditor?.errors || []
})

const scopeAuditor = computed(() => outputs.value?.scope_auditor || outputs.value?.['scope_auditor'])

function fetchRun(runId: number) {
  return HomeworkWorkflowApi.get(runId).then(r => {
    nodes.value = toDagNodes((r.nodes || []) as any)
    runDetail.value = r.run
    if (r.run?.output_json) outputs.value = safeJson<Record<string, any>>(r.run.output_json, null) || outputs.value
  })
}

function startPolling(runId: number) {
  stopPolling()
  pollTimer.value = window.setInterval(async () => {
    await fetchRun(runId)
    if (runDetail.value && runDetail.value.status !== 'running') stopPolling()
  }, 1500)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

async function runFlow() {
  error.value = ''
  if (!homeworkText.value.trim()) {
    error.value = '请粘贴作业内容。'
    return
  }
  running.value = true
  nodes.value = []
  outputs.value = null
  runDetail.value = null
  try {
    const r = await HomeworkWorkflowApi.run({
      course_id: courseId.value || undefined,
      chapter_id: chapterId.value || undefined,
      lesson_id: lessonId.value || undefined,
      homework_text: homeworkText.value,
    })
    await fetchRun(r.run_id)
    startPolling(r.run_id)
    toast.success(`已启动作业流 #${r.run_id}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    toast.error(`运行失败:${e?.message || e}`)
  } finally {
    running.value = false
  }
}

function riskVariant(risk?: string): 'green' | 'amber' | 'red' {
  if (!risk) return 'amber'
  const r = risk.toLowerCase()
  if (r === 'high') return 'red'
  if (r === 'low') return 'green'
  return 'amber'
}

onBeforeUnmount(() => {
  stopPolling()
})

onMounted(loadMeta)
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="✏️"
      title="作业流"
      subtitle="粘贴或输入作业内容，运行风险分级 → 解题 → 高风险题并行求解（MiniMax）→ 解题解释 → 证据 / 范围审查。"
    >
      <template #actions>
        <button class="btn btn-primary" :disabled="running" @click="runFlow">
          {{ running ? '运行中...' : '运行作业流' }}
        </button>
      </template>
    </PageHeader>

    <section class="section">
      <div class="grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        <div>
          <label class="label">作业内容</label>
          <textarea v-model="homeworkText" rows="10" class="input"
            placeholder="支持多道题一起粘贴，按编号 / 空行 / 标点切题。建议至少 1 道完整题。" />
        </div>
        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">课程</label>
            <select v-model="courseId" class="input">
              <option value="">未指定</option>
              <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
            </select>
          </div>
          <div>
            <label class="label">章节</label>
            <select v-model="chapterId" class="input" :disabled="!courseId">
              <option value="">未指定</option>
              <option v-for="ch in filteredChapters" :key="ch.id" :value="ch.id">
                {{ ch.chapter_no ? `第${ch.chapter_no}章 ` : '' }}{{ ch.title }}
              </option>
            </select>
          </div>
          <div>
            <label class="label">课时</label>
            <select v-model="lessonId" class="input" :disabled="!courseId">
              <option value="">未指定</option>
              <option v-for="l in filteredLessons" :key="l.id" :value="l.id">
                {{ l.lesson_no || `L${l.id}` }}
              </option>
            </select>
          </div>
        </div>
      </div>
      <p v-if="error" class="mt-2 text-xs text-rose-300">{{ error }}</p>
    </section>

    <States :loading="running" :error="error" :empty="!runDetail && !nodes.length" empty-icon="✏️" empty-title="还没有作业流结果">
      <section v-if="runDetail" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">DAG 节点流程</h3>
            <p class="card-muted">Run #{{ runDetail.id }} · 模式：{{ runDetail.mode }}</p>
          </div>
          <StatusBadge :status="runDetail.status" />
        </div>
        <DagFlow :nodes="nodes" :loading="runDetail.status === 'running'" />
      </section>

      <section v-if="nodes.length" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">节点详情</h3>
            <p class="card-muted">点击节点查看模型、token、错误信息。</p>
          </div>
        </div>
        <NodeDetail :nodes="nodes" />
      </section>

      <!-- 风险分级 -->
      <section v-if="riskItems.length" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">风险分级（risk_classifier）</h3>
            <p class="card-muted">高风险题会触发并行求解与更严格的审查。</p>
          </div>
        </div>
        <ul class="space-y-2">
          <li v-for="r in riskItems" :key="r.index" class="card-soft">
            <div class="flex items-center justify-between gap-2">
              <p class="text-sm font-medium text-slate-100">第 {{ r.index }} 题</p>
              <StatusBadge :status="r.risk" :variant="riskVariant(r.risk)">{{ r.risk }}</StatusBadge>
            </div>
            <p class="mt-1 text-xs text-slate-400">{{ r.text }}</p>
            <p v-if="r.reason?.length" class="mt-1 text-[11px] text-slate-500">
              原因：{{ r.reason.join('、') }}
            </p>
          </li>
        </ul>
      </section>

      <!-- 题目求解 -->
      <section v-if="solves.length" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">题目求解（solver）</h3>
            <p class="card-muted">每道题的最终答案、解题计划、详细解答、模型来源。</p>
          </div>
        </div>
        <div class="space-y-2">
          <details v-for="s in solves" :key="s.question_no" class="card-soft">
            <summary class="flex cursor-pointer items-center justify-between gap-2">
              <span class="text-sm font-semibold text-slate-100">第 {{ s.question_no }} 题</span>
              <div class="flex items-center gap-2">
                <StatusBadge v-if="s.risk?.risk" :status="s.risk.risk" :variant="riskVariant(s.risk.risk)">
                  {{ s.risk.risk }}
                </StatusBadge>
                <span class="font-mono text-[11px] text-slate-500">{{ s.model_used || '—' }}</span>
                <span v-if="typeof s.confidence === 'number'" class="text-[11px] text-slate-500">
                  置信度 {{ (s.confidence * 100).toFixed(0) }}%
                </span>
              </div>
            </summary>
            <div class="mt-3 space-y-2 text-xs text-slate-300">
              <div>
                <p class="text-slate-500">最终答案：</p>
                <p class="font-mono text-emerald-300">{{ s.final_answer || '（无）' }}</p>
              </div>
              <div v-if="s.solution_plan">
                <p class="text-slate-500">解题计划：</p>
                <p class="whitespace-pre-line text-slate-300">{{ s.solution_plan }}</p>
              </div>
              <details v-if="s.detailed_solution">
                <summary class="cursor-pointer text-slate-400">详细解答</summary>
                <pre class="code-panel mt-2 max-h-72">{{ s.detailed_solution }}</pre>
              </details>
            </div>
          </details>
        </div>
      </section>

      <!-- 高风险并行 -->
      <section v-if="parallelSolves.length" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">高风险并行求解（MiniMax M3）</h3>
            <p class="card-muted">对高风险题使用独立模型并行求解，用于交叉验证。</p>
          </div>
        </div>
        <ul class="space-y-2">
          <li v-for="(s, i) in parallelSolves" :key="i" class="card-soft">
            <p class="text-sm font-medium text-slate-100">题号 {{ s.question_no || (i + 1) }}</p>
            <p class="mt-1 text-xs text-slate-400">{{ s.final_answer || '（无答案）' }}</p>
            <p v-if="s.model_used" class="mt-1 text-[11px] text-slate-500">模型：{{ s.model_used }}</p>
          </li>
        </ul>
      </section>

      <!-- provisional 错题提示 -->
      <section v-if="provisionalErrors.length" class="section border-rose-800/40 bg-rose-950/30">
        <div class="section-head">
          <div>
            <h3 class="card-title text-rose-200">Provisional 错题</h3>
            <p class="card-muted text-rose-300/80">证据 / 范围审查节点判定存在错题风险，可前往「错题确认」做最终确认。</p>
          </div>
          <StatusBadge status="provisional" variant="purple">{{ provisionalErrors.length }} 条</StatusBadge>
        </div>
        <ul class="space-y-2">
          <li v-for="(p, i) in provisionalErrors" :key="String(i)" class="rounded-lg bg-rose-950/50 p-3 text-xs text-rose-100">
            <p class="font-semibold">{{ p.question_text || p.title || ('错题 #' + String(Number(i) + 1)) }}</p>
            <p v-if="p.reason" class="mt-1 text-rose-200/80">{{ p.reason }}</p>
          </li>
        </ul>
      </section>

      <!-- 范围审查 -->
      <section v-if="scopeAuditor" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">范围审查</h3>
            <p class="card-muted">scope_auditor 节点：是否超出当前课程范围。</p>
          </div>
          <StatusBadge :status="scopeAuditor.out_of_scope ? 'out_of_scope' : 'in_scope'"
                       :variant="scopeAuditor.out_of_scope ? 'amber' : 'green'">
            {{ scopeAuditor.out_of_scope ? '超出范围' : '在范围内' }}
          </StatusBadge>
        </div>
        <div v-if="scopeAuditor" class="space-y-2 text-xs text-slate-300">
          <div v-if="scopeAuditor.in_scope !== undefined || scopeAuditor.out_of_scope !== undefined">
            <p class="text-slate-500">范围判断：</p>
            <p class="mt-1">
              <span :class="scopeAuditor.out_of_scope ? 'text-amber-300' : 'text-emerald-300'">
                {{ scopeAuditor.out_of_scope ? '超出范围' : '在范围内' }}
              </span>
            </p>
          </div>
          <div v-if="Array.isArray(scopeAuditor.in_scope) && scopeAuditor.in_scope.length">
            <p class="text-slate-500">范围内：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5">
              <li v-for="(x, i) in scopeAuditor.in_scope" :key="`in-${i}`">{{ typeof x === 'string' ? x : prettyJson(x) }}</li>
            </ul>
          </div>
          <div v-if="Array.isArray(scopeAuditor.out_of_scope) && scopeAuditor.out_of_scope.length">
            <p class="text-slate-500">超出范围：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5">
              <li v-for="(x, i) in scopeAuditor.out_of_scope" :key="`out-${i}`">{{ typeof x === 'string' ? x : prettyJson(x) }}</li>
            </ul>
          </div>
          <div v-if="Array.isArray(scopeAuditor.evidence) && scopeAuditor.evidence.length">
            <p class="text-slate-500">证据：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5">
              <li v-for="(x, i) in scopeAuditor.evidence" :key="`ev-${i}`">{{ typeof x === 'string' ? x : prettyJson(x) }}</li>
            </ul>
          </div>
          <div v-if="scopeAuditor.reasoning || scopeAuditor.reason">
            <p class="text-slate-500">理由：</p>
            <p class="mt-1 whitespace-pre-wrap text-slate-300">{{ scopeAuditor.reasoning || scopeAuditor.reason }}</p>
          </div>
          <details v-if="scopeAuditor && (Object.keys(scopeAuditor).filter(k => !['in_scope','out_of_scope','evidence','reasoning','reason'].includes(k)).length)">
            <summary class="cursor-pointer text-slate-400">其他字段</summary>
            <pre class="code-panel mt-2 whitespace-pre-wrap">{{ prettyJson(scopeAuditor) }}</pre>
          </details>
          <pre v-if="!scopeAuditor.in_scope && !scopeAuditor.out_of_scope && !scopeAuditor.evidence && !scopeAuditor.reasoning && !scopeAuditor.reason" class="code-panel whitespace-pre-wrap">{{ prettyJson(scopeAuditor) }}</pre>
        </div>
      </section>
    </States>
  </div>
</template>
