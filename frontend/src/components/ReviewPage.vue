<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArtifactsApi, ChaptersApi, CoursesApi, ExamsApi, ReviewWorkflowApi, RunsApi } from '../api/endpoints'
import type { Chapter, Course, ExamEvent, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import { toDagNodes } from './widgets/dag-types'
import { fmtDate, prettyJson, safeJson } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const exams = ref<ExamEvent[]>([])

const kind = ref<'chapter' | 'exam'>('chapter')
const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const examId = ref<number | ''>('')

const running = ref(false)
const error = ref('')
const runDetail = ref<WorkflowRun | null>(null)
const nodes = ref<ReturnType<typeof toDagNodes>>([])
const outputs = ref<Record<string, any> | null>(null)
const pollTimer = ref<number | null>(null)
const toast = useToast()

async function loadMeta() {
  try {
    const [cs, chs, es] = await Promise.all([CoursesApi.list(), ChaptersApi.list(), ExamsApi.list()])
    courses.value = cs
    chapters.value = chs
    exams.value = es
    if (!courseId.value && cs.length) courseId.value = cs[0].id
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

const filteredChapters = computed(() =>
  courseId.value ? chapters.value.filter(c => c.course_id === Number(courseId.value)) : [],
)

const upcomingExams = computed(() => {
  const today = new Date().toISOString().slice(0, 10)
  return exams.value
    .filter(e => e.date && e.date >= today)
    .sort((a, b) => (a.date! < b.date! ? -1 : 1))
})

const reviewPackage = computed(() => outputs.value?.writer?.review_package || outputs.value?.writer || null)
const artifact = computed(() => outputs.value?.render_document?.artifact || null)
const selfTest = computed(() => outputs.value?.self_test || null)

const resources = computed(() => {
  const r: any = outputs.value?.aggregator?.resources || outputs.value?.aggregator || {}
  return {
    lessons: Array.isArray(r.lessons) ? r.lessons : [],
    notes: Array.isArray(r.notes) ? r.notes : [],
    chunk_count: typeof r.chunk_count === 'number' ? r.chunk_count : 0,
    confirmed_errors: Array.isArray(r.confirmed_errors) ? r.confirmed_errors : (Array.isArray(r.errors) ? r.errors : []),
  }
})

function normalizeSelfTest(raw: any): Array<{ q: string; a: string; e?: string }> {
  const list: any[] = Array.isArray(raw) ? raw
    : Array.isArray(raw?.questions) ? raw.questions
    : Array.isArray(raw?.items) ? raw.items
    : []
  return list.map((it: any) => ({
    q: String(it?.question || it?.prompt || it?.stem || ''),
    a: String(it?.answer || it?.solution || it?.correct_answer || ''),
    e: it?.explanation || it?.rationale || it?.analysis || undefined,
  })).filter(it => it.q || it.a)
}

const selfTestQuestions = computed(() => normalizeSelfTest(selfTest.value))

function normalizeMaterials(raw: any): { kind: 'list' | 'text' | 'empty'; items: string[]; text: string } {
  if (!raw) return { kind: 'empty', items: [], text: '' }
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      return normalizeMaterials(parsed)
    } catch {
      return { kind: 'text', items: [], text: raw }
    }
  }
  const list: any[] = Array.isArray(raw) ? raw
    : Array.isArray(raw?.items) ? raw.items
    : Array.isArray(raw?.chapters) ? raw.chapters
    : []
  if (list.length) {
    return { kind: 'list', items: list.map((it: any) => typeof it === 'string' ? it : (it?.title || it?.name || JSON.stringify(it))), text: '' }
  }
  if (typeof raw.markdown === 'string') return { kind: 'text', items: [], text: raw.markdown }
  if (typeof raw.body === 'string') return { kind: 'text', items: [], text: raw.body }
  return { kind: 'empty', items: [], text: '' }
}

const reviewMaterials = computed(() => normalizeMaterials(reviewPackage.value?.review_materials))

async function run() {
  error.value = ''
  running.value = true
  nodes.value = []
  outputs.value = null
  runDetail.value = null
  try {
    const exam = exams.value.find(e => e.id === Number(examId.value))
    const scope = exam
      ? { course_id: exam.course_id, exam_id: exam.id, title: exam.title, date: exam.date }
      : undefined
    const r = await ReviewWorkflowApi.run({
      kind: kind.value,
      course_id: courseId.value || undefined,
      chapter_id: chapterId.value || undefined,
      scope,
      exam_date: exam?.date,
    })
    await fetchRun(r.run_id)
    startPolling(r.run_id)
    toast.success(`已启动复习流 #${r.run_id}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    toast.error(`运行失败:${e?.message || e}`)
  } finally {
    running.value = false
  }
}

function fetchRun(id: number) {
  return RunsApi.get(id).then(r => {
    nodes.value = toDagNodes((r.nodes || []) as any)
    runDetail.value = r.run
    if (r.run?.output_json) outputs.value = safeJson<Record<string, any>>(r.run.output_json, null) || outputs.value
  })
}

function startPolling(id: number) {
  stopPolling()
  pollTimer.value = window.setInterval(async () => {
    await fetchRun(id)
    if (runDetail.value && runDetail.value.status !== 'running') stopPolling()
  }, 1500)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

onBeforeUnmount(() => {
  stopPolling()
})

onMounted(loadMeta)
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="repeat"
      title="复习中心"
      subtitle="章末复习 / 考前复习：聚合笔记、错题与材料，生成复习大纲、自测题与复习计划。"
    >
      <template #actions>
        <button class="btn btn-primary" :disabled="running" @click="run">
          {{ running ? '运行中...' : '运行复习流' }}
        </button>
      </template>
    </PageHeader>

    <section class="section">
      <div class="flex flex-wrap items-center gap-4">
        <div class="flex items-center gap-2">
          <label class="flex items-center gap-1 text-sm text-slate-300">
            <input type="radio" value="chapter" v-model="kind" /> 章末复习
          </label>
          <label class="flex items-center gap-1 text-sm text-slate-300">
            <input type="radio" value="exam" v-model="kind" /> 考前复习
          </label>
        </div>
        <div>
          <label class="label">课程</label>
          <select v-model="courseId" class="input !w-48">
            <option value="">未指定</option>
            <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
        </div>
        <div v-if="kind === 'chapter'">
          <label class="label">章节</label>
          <select v-model="chapterId" class="input !w-56" :disabled="!courseId">
            <option value="">未指定</option>
            <option v-for="ch in filteredChapters" :key="ch.id" :value="ch.id">
              {{ ch.chapter_no ? `第${ch.chapter_no}章 ` : '' }}{{ ch.title }}
            </option>
          </select>
        </div>
        <div v-else>
          <label class="label">即将到来的考试</label>
          <select v-model="examId" class="input !w-72" :disabled="!upcomingExams.length">
            <option value="">未指定</option>
            <option v-for="e in upcomingExams" :key="e.id" :value="e.id">
              {{ e.date }} · {{ e.course_name || `课程 #${e.course_id}` }} · {{ e.title }}
            </option>
          </select>
        </div>
      </div>
      <p v-if="error" class="mt-2 text-xs text-[var(--acc-red)]">{{ error }}</p>
    </section>

    <States :loading="running" :error="error" :empty="!runDetail && !nodes.length" empty-icon="repeat" empty-title="还没有复习流结果">
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

      <section v-if="reviewPackage" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">复习大纲 / 复习材料</h3>
            <p class="card-muted">review_writer 节点输出。</p>
          </div>
        </div>
        <div class="grid gap-3 lg:grid-cols-2">
          <div>
            <p class="text-xs text-slate-400">大纲：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-200">
              <li v-for="(o, i) in reviewPackage.outline || []" :key="i">{{ o }}</li>
            </ul>
          </div>
          <div>
            <p class="text-xs text-slate-400">复习材料：</p>
            <ul v-if="reviewMaterials.kind === 'list'" class="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-200">
              <li v-for="(item, i) in reviewMaterials.items" :key="i">{{ item }}</li>
            </ul>
            <pre v-else-if="reviewMaterials.kind === 'text'" class="code-panel whitespace-pre-wrap">{{ reviewMaterials.text }}</pre>
            <pre v-else class="code-panel whitespace-pre-wrap">{{ prettyJson(reviewPackage.review_materials || reviewPackage) }}</pre>
          </div>
        </div>
        <div v-if="artifact" class="mt-4 flex flex-wrap gap-2 border-t border-slate-700 pt-4">
          <a class="btn btn-primary" :href="ArtifactsApi.previewUrl(artifact.id)" target="_blank" rel="noopener">预览排版</a>
          <a class="btn btn-secondary" :href="ArtifactsApi.downloadUrl(artifact.id, 'html')">下载 HTML</a>
          <a v-if="artifact.has_pdf" class="btn btn-secondary" :href="ArtifactsApi.downloadUrl(artifact.id, 'pdf')">下载 PDF</a>
        </div>
      </section>

      <section v-if="resources" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">资源聚合（aggregator）</h3>
            <p class="card-muted">本地 SQL 聚合：课时、笔记、错题、原始材料分块。</p>
          </div>
        </div>
        <div class="grid gap-3 md:grid-cols-4">
          <div class="card-soft !p-3">
            <p class="text-xs text-slate-400">课时</p>
            <p class="text-xl font-bold text-slate-100">{{ resources.lessons.length }}</p>
          </div>
          <div class="card-soft !p-3">
            <p class="text-xs text-slate-400">笔记</p>
            <p class="text-xl font-bold text-slate-100">{{ resources.notes.length }}</p>
          </div>
          <div class="card-soft !p-3">
            <p class="text-xs text-slate-400">错题</p>
            <p class="text-xl font-bold text-slate-100">{{ resources.confirmed_errors.length }}</p>
          </div>
          <div class="card-soft !p-3">
            <p class="text-xs text-slate-400">切块</p>
            <p class="text-xl font-bold text-slate-100">{{ resources.chunk_count }}</p>
          </div>
        </div>
        <p v-if="!resources.lessons.length && !resources.notes.length && !resources.confirmed_errors.length && !resources.chunk_count" class="mt-3 text-xs text-slate-500">
          暂无聚合数据。
        </p>
        <div v-if="resources.confirmed_errors.length" class="mt-3">
          <p class="text-xs text-slate-400">错题热点：</p>
          <ul class="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-200">
            <li v-for="(e, i) in resources.confirmed_errors.slice(0, 8)" :key="i">
              {{ (e.question_text || '').slice(0, 60) }}
              <span class="text-[11px] text-slate-500"> · {{ fmtDate(e.created_at) }}</span>
            </li>
          </ul>
        </div>
      </section>

      <section v-if="selfTest" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">自测题</h3>
            <p class="card-muted">self_test_writer 节点输出。</p>
          </div>
        </div>
        <ol v-if="selfTestQuestions.length" class="space-y-3 list-decimal pl-5">
          <li v-for="(q, i) in selfTestQuestions" :key="i">
            <p class="text-slate-100">{{ q.q || '（题目缺失）' }}</p>
            <p class="mt-1 text-[var(--acc-green)]">答案:{{ q.a || '—' }}</p>
            <p v-if="q.e" class="mt-1 text-slate-400 text-xs">解析:{{ q.e }}</p>
          </li>
        </ol>
        <pre v-else class="code-panel whitespace-pre-wrap">{{ prettyJson(selfTest) }}</pre>
      </section>
    </States>
  </div>
</template>
