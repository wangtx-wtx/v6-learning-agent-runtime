<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArtifactsApi, CapabilitiesApi, ChaptersApi, CoursesApi, LessonWorkflowApi, LessonsApi, MaterialsApi, RunsApi } from '../api/endpoints'
import type { Chapter, CognitiveMapView, Course, CoverageView, EvidenceV2View, Lesson, Material, MaterialDomainView, QualityStateView, SegmentsView, SourceSpanView, UnderstandingView, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import EvidenceV2Panel from './widgets/EvidenceV2Panel.vue'
import { toDagNodes } from './widgets/dag-types'
import { safeJson, statusLabel, truncate } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])

const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const lessonId = ref<number | ''>('')
const transcript = ref('')
const limits = ref({
  max_upload_bytes: 50 * 1024 * 1024,
  max_inline_transcript_chars: 2_000_000,
  transcript_warning_chars: 200_000,
})
const transcriptChars = computed(() => transcript.value.length)
const transcriptTooLarge = computed(() => transcriptChars.value > limits.value.max_inline_transcript_chars)
const transcriptIsLarge = computed(() => transcriptChars.value > limits.value.transcript_warning_chars)
function localDateValue(d = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
const date = ref(localDateValue())
// 桌面端材料选择（方案 13.5）：可勾选已解析材料注入听课流（material_ids）
const materials = ref<Material[]>([])
const selectedMaterialIds = ref<number[]>([])

const running = ref(false)
const error = ref('')
const currentRunId = ref<number | null>(null)
const nodes = ref<ReturnType<typeof toDagNodes>>([])
const runDetail = ref<WorkflowRun | null>(null)
const outputs = ref<Record<string, any> | null>(null)
const pollTimer = ref<number | null>(null)
const pollBusy = ref(false)
const toast = useToast()

// ---- V6 Learning Engine Phase 1：材料域 / 覆盖账本 ----
const domainView = ref<MaterialDomainView | null>(null)
const coverageView = ref<CoverageView | null>(null)
const segmentsView = ref<SegmentsView | null>(null)
const understandingView = ref<UnderstandingView | null>(null)
const cognitiveView = ref<CognitiveMapView | null>(null)
// V6 Phase 4：Evidence V2（只读审计）
const evidenceView = ref<EvidenceV2View | null>(null)
const qualityView = ref<QualityStateView | null>(null)
const evidenceBusy = ref(false)
const evidenceError = ref('')
const expandedSegmentId = ref<number | null>(null)
const coverageError = ref('')
const expandedReason = ref<string | null>(null)
const expandedItemId = ref<number | null>(null)
const spanDetail = ref<SourceSpanView | null>(null)
const spanError = ref('')

async function loadCoverage(id: number) {
  coverageError.value = ''
  // 旧运行（V5 或 V6 之前的 run）没有这些数据：404 属于正常语义，不当作错误提示。
  const [d, c, s, u] = await Promise.allSettled([
    LessonWorkflowApi.materialDomain(id),
    LessonWorkflowApi.coverage(id),
    LessonWorkflowApi.segments(id),
    LessonWorkflowApi.understanding(id),
  ])
  domainView.value = d.status === 'fulfilled' ? d.value : null
  coverageView.value = c.status === 'fulfilled' ? c.value : null
  segmentsView.value = s.status === 'fulfilled' ? s.value : null
  understandingView.value = u.status === 'fulfilled' ? u.value : null
  try {
    cognitiveView.value = await LessonWorkflowApi.cognitiveMap(id)
  } catch {
    cognitiveView.value = null
  }
  await loadEvidence(id)
  try {
    qualityView.value = await LessonWorkflowApi.quality(id)
  } catch {
    qualityView.value = null
  }
  if (d.status === 'rejected' && c.status === 'rejected') {
    coverageError.value = '该运行没有 Material Domain / 覆盖报告（可能是 V5 运行或 V6_LEARNING_ENGINE=off）。'
  }
}

// ---- Phase 4：Evidence V2（只读审计）----
async function loadEvidence(id: number | null) {
  if (!id) {
    evidenceView.value = null
    return
  }
  evidenceBusy.value = true
  evidenceError.value = ''
  try {
    evidenceView.value = await LessonWorkflowApi.evidenceV2(id)
  } catch (e: any) {
    evidenceView.value = null
    // 404 是正常语义（V5 运行 / 尚未绑定），不作为错误提示。
    const status = e?.response?.status
    if (status && status !== 404) {
      evidenceError.value = '读取 Evidence V2 失败（HTTP ' + status + '）。'
    }
  } finally {
    evidenceBusy.value = false
  }
}

// ---- Phase 3：认知分析 ----
const cognitiveMap = computed(() => cognitiveView.value?.cognitive_map || null)
const cognitiveItems = computed(() => cognitiveView.value?.items || [])
const COGNITIVE_TYPE_LABEL: Record<string, string> = {
  confusion_point: '易混淆',
  prerequisite_gap: '前置缺口',
  pitfall: '易错点',
  emphasis: '教师强调',
  concept_relation: '概念关系',
  memory_anchor: '记忆锚点',
  missing_step: '缺失步骤',
  difficulty: '理解难度',
}
const cognitiveTypeOrder = Object.keys(COGNITIVE_TYPE_LABEL)
const cognitiveGrouped = computed(() => {
  const groups: Record<string, typeof cognitiveItems.value> = {}
  for (const item of cognitiveItems.value) {
    const key = item.item_type || 'other'
    if (!groups[key]) groups[key] = []
    groups[key].push(item)
  }
  return cognitiveTypeOrder
    .filter(t => groups[t]?.length)
    .map(t => ({ type: t, label: COGNITIVE_TYPE_LABEL[t], items: groups[t] }))
})
const ORIGIN_LABEL: Record<string, string> = {  classroom_evidence: '课堂证据',
  confirmed_error: '已确认错题',
  model_cognitive_inference: '模型推断',
  chapter_note: '章节备注',
  mastery_signal: 'mastery 信号',
  homework_feedback: '作业反馈',
}
function originLabel(o: string) {
  return ORIGIN_LABEL[o] || o
}

const segments = computed(() => segmentsView.value?.segments || [])
const lessonUnderstanding = computed(() => understandingView.value?.lesson_understanding || null)
const lessonKnowledgeUnits = computed<Array<Record<string, any>>>(() =>
  (lessonUnderstanding.value?.understanding?.knowledge_units as Array<Record<string, any>>) || [])
const lessonConflicts = computed<string[]>(() =>
  (lessonUnderstanding.value?.understanding?.unresolved_conflicts as string[]) || [])

function segmentUnderstanding(segmentId: number) {
  return (understandingView.value?.segment_understandings || [])
    .find(s => s.segment_id === segmentId) || null
}
function toggleSegment(id: number) {
  expandedSegmentId.value = expandedSegmentId.value === id ? null : id
}
function fmtDuration(ms: unknown): string {
  const n = typeof ms === 'number' ? ms : Number(ms)
  if (!Number.isFinite(n) || n <= 0) return '—'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`
}

const coverageMetrics = computed(() => coverageView.value?.report?.metrics || null)
const gate = computed(() => coverageView.value?.report?.gate || null)
const degradedReason = computed(() => coverageView.value?.report?.degradation_reason || '')

function pct(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(1)}%`
}
function num(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? String(n) : '—'
}
function msToClock(ms: unknown): string {
  const n = typeof ms === 'number' ? ms : Number(ms)
  if (!Number.isFinite(n) || n < 0) return '—'
  const total = Math.floor(n / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}
function toggleReason(code: string | null) {
  const key = code || '(no reason)'
  expandedReason.value = expandedReason.value === key ? null : key
}
/** 展开某个 domain item 的全部 Source ID（默认只显示前 3 个）。 */
function toggleItemSpans(domainItemId: number) {
  expandedItemId.value = expandedItemId.value === domainItemId ? null : domainItemId
}
/** 按 Source ID 取原文（必须带 run_id，避免跨课程越权读取）。 */
async function openSpan(sourceId: string) {
  if (!currentRunId.value) return
  spanError.value = ''
  spanDetail.value = null
  try {
    spanDetail.value = await LessonWorkflowApi.sourceSpan(sourceId, currentRunId.value)
    spanCache.value = { ...spanCache.value, [sourceId]: spanDetail.value.text || '' }
  } catch (e: any) {
    spanError.value = e?.message || String(e)
  }
}
function closeSpan() {
  spanDetail.value = null
  spanError.value = ''
}

/**
 * Evidence V2 面板：已通过 openSpan 读取过的来源原文（按 Source ID 缓存）。
 * 只用于「实时读取」对照展示 —— 面板显示的 bound_quote 本身来自后端数据库。
 */
const spanCache = ref<Record<string, string>>({})
function evidenceSpanText(sourceId: string): string | null {
  const cached = spanCache.value[sourceId]
  if (cached == null) return null
  return cached.length > 120 ? cached.slice(0, 120) + '…' : cached
}

/** 覆盖账本汇总（stage + outcome → 数量），用于展示每个阶段的实际处理量。 */
const ledgerRows = computed(() => {
  const rows = (coverageView.value?.ledger || []).map(r => ({
    key: `${r.stage}-${r.outcome}-${r.reason_code || ''}`,
    stage: r.stage,
    outcome: r.outcome,
    reason: r.reason_code,
    n: Number(r.n) || 0,
  }))
  return rows.sort((a, b) => (a.stage === b.stage ? b.n - a.n : a.stage.localeCompare(b.stage)))
})

/** 必需材料的具体失败原因（Phase 2 要求前端可见，不能只给一个 gate）。 */
const requiredFailures = computed<string[]>(() => {
  const m = coverageView.value?.report?.metrics
  if (!m) return []
  const out: string[] = []
  if (Number(m.required_items_failed || 0) > 0) out.push(`必需材料解析失败：${m.required_items_failed} 个`)
  if (Number(m.required_items_unsupported || 0) > 0) out.push(`必需材料类型不支持：${m.required_items_unsupported} 个`)
  if (Number(m.required_items_without_canonical_span || 0) > 0)
    out.push(`必需材料没有可理解的 canonical span：${m.required_items_without_canonical_span} 个`)
  if (Number(m.required_spans_unprocessed || 0) > 0) out.push(`必需材料 span 未进入理解：${m.required_spans_unprocessed} 个`)
  if (Number(m.unassigned_count || 0) > 0) out.push(`未分配到 segment 的 span：${m.unassigned_count} 个`)
  if (Number(m.silent_dropped || 0) > 0) out.push(`silent drop：${m.silent_dropped} 个`)
  if (Number(m.source_refs_invalid || 0) > 0) out.push(`无效 source ref：${m.source_refs_invalid} 个`)
  if (Number(m.segment_failed_count || 0) > 0) out.push(`理解失败的 segment：${m.segment_failed_count} 个`)
  return out
})

async function loadMeta() {
  try {
    const [cs, chs, ls] = await Promise.all([
      CoursesApi.list(),
      ChaptersApi.list(),
      LessonsApi.list(),
    ])
    courses.value = cs
    chapters.value = chs
    lessons.value = ls
    if (!courseId.value && cs.length) courseId.value = cs[0].id
    // 已解析/解析中材料列表（供材料勾选）
    try {
      materials.value = await MaterialsApi.list()
    } catch { /* 材料列表失败不阻塞主流程 */ }
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

const parsedMaterials = computed(() =>
  materials.value.filter(m =>
    ['parsed', 'ready'].includes((m.parser_status || m.status || '').toLowerCase())
    && (!courseId.value || m.course_id === Number(courseId.value)),
  ),
)

function onCourseChange() {
  chapterId.value = ''
  lessonId.value = ''
  selectedMaterialIds.value = []
}

function onChapterChange() {
  lessonId.value = ''
}

function toggleMaterial(id: number) {
  const i = selectedMaterialIds.value.indexOf(id)
  if (i >= 0) selectedMaterialIds.value.splice(i, 1)
  else selectedMaterialIds.value.push(id)
}

const filteredChapters = computed(() =>
  courseId.value ? chapters.value.filter(c => c.course_id === Number(courseId.value)) : [],
)
const filteredLessons = computed(() => {
  if (chapterId.value) return lessons.value.filter(l => l.chapter_id === Number(chapterId.value))
  if (courseId.value) return lessons.value.filter(l => l.course_id === Number(courseId.value))
  return []
})

async function runFlow() {
  error.value = ''
  if (!transcript.value.trim()) {
    error.value = '请粘贴课堂转写文本。'
    return
  }
  if (transcriptTooLarge.value) {
    error.value = `文本共 ${transcriptChars.value.toLocaleString()} 字，超过单次粘贴上限。请保存为 UTF-8 TXT/SRT/VTT 后在“材料收件箱”上传。`
    return
  }
  running.value = true
  nodes.value = []
  outputs.value = null
  runDetail.value = null
  try {
    const course = courses.value.find(c => c.id === Number(courseId.value))
    const chapter = chapters.value.find(c => c.id === Number(chapterId.value))
    const lesson = lessons.value.find(l => l.id === Number(lessonId.value))
    const r = await LessonWorkflowApi.run({
      course_id: courseId.value || undefined,
      chapter_id: chapterId.value || undefined,
      lesson_id: lessonId.value || undefined,
      course: course?.name,
      chapter: chapter?.title,
      lesson_no: lesson?.lesson_no,
      date: date.value,
      transcript: transcript.value,
      material_ids: selectedMaterialIds.value.length ? [...selectedMaterialIds.value] : undefined,
    })
    currentRunId.value = r.run_id
    await fetchRun(r.run_id, false)
    startPolling(r.run_id)
    toast.success(`已启动听课流 #${r.run_id}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    toast.error(`运行失败:${e?.message || e}`)
  } finally {
    running.value = false
  }
}

async function fetchRun(id: number, detailed = true) {
  try {
    const r = await LessonWorkflowApi.get(id, detailed)
    if (detailed) error.value = ''
    nodes.value = toDagNodes((r.nodes || []) as any)
    runDetail.value = r.run
    if (r.run?.output_json) {
      outputs.value = safeJson(r.run.output_json, null) || outputs.value
    }
    if (detailed) await loadCoverage(id)
    return r.run?.status || ''
  } catch (e: any) {
    if (detailed) error.value = e?.message || String(e)
    return ''
  }
}

function startPolling(id: number) {
  stopPolling()
  pollTimer.value = window.setInterval(async () => {
    if (pollBusy.value) return
    pollBusy.value = true
    try {
      const status = await fetchRun(id, false)
      if (status && !['queued', 'running'].includes(status)) {
        stopPolling()
        await fetchRun(id, true)
      }
    } finally {
      pollBusy.value = false
    }
  }, 1500)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

const noteNode = computed(() => nodes.value.find(n => n.name === 'note_writer'))
const noteData = computed(() => {
  if (!noteNode.value?.output_ref) return null
  return safeJson<any>(noteNode.value.output_ref, null)
})
const publishNode = computed(() => nodes.value.find(n => n.name === 'publish'))
const publishPath = computed(() => {
  if (!publishNode.value?.output_ref) return null
  const obj = safeJson<any>(publishNode.value.output_ref, null)
  return obj?.markdown_path || null
})
const artifact = computed(() => outputs.value?.render_document?.artifact || null)

function outputKey(key: string) {
  if (!outputs.value) return null
  return outputs.value[key] || null
}

onBeforeUnmount(() => {
  stopPolling()
})

onMounted(async () => {
  await Promise.all([
    loadMeta(),
    CapabilitiesApi.limits().then(v => { limits.value = v }).catch(() => {}),
  ])
  try {
    const r = await RunsApi.list(5)
    const lessonRuns = r.filter(x => x.workflow === 'lesson')
    if (lessonRuns.length && !currentRunId.value) {
      currentRunId.value = lessonRuns[0].id
      await fetchRun(lessonRuns[0].id)
    }
  } catch {
    /* ignore */
  }
})
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="headphones"
      title="听课流"
      subtitle="粘贴课堂转写文本，运行本地多智能体流水线：转写切段 → 结构化 → 学生视角模拟 → 笔记写作 → 独立审查 → 证据审计 → 发布到 Obsidian。"
    >
      <template #actions>
        <button type="button" class="btn btn-secondary" :disabled="running || !currentRunId" @click="currentRunId && fetchRun(currentRunId)">
          刷新结果
        </button>
        <button type="button" class="btn btn-primary" :disabled="running || transcriptTooLarge" @click="runFlow">
          {{ running ? '运行中...' : '运行听课流' }}
        </button>
      </template>
    </PageHeader>

    <section class="section">
      <div class="grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        <div>
          <label class="label">课堂转写文本</label>
          <textarea
            v-model="transcript"
            rows="8"
            class="input"
            placeholder="粘贴课堂录音转写文本或课堂笔记，至少 1 段。"
          />
          <div class="mt-1 flex items-start justify-between gap-3 text-[11px]">
            <p :class="transcriptTooLarge ? 'text-[var(--acc-red)]' : transcriptIsLarge ? 'text-[var(--acc-orange)]' : 'text-slate-500'">
              {{ transcriptChars.toLocaleString() }} / {{ limits.max_inline_transcript_chars.toLocaleString() }} 字
            </p>
            <p v-if="transcriptTooLarge" class="text-right text-[var(--acc-red)]">
              已超过粘贴上限，请保存为 TXT/SRT/VTT 后从材料收件箱上传。
            </p>
            <p v-else-if="transcriptIsLarge" class="text-right text-[var(--acc-orange)]">
              长文本会自动按模型上下文安全分段，处理时间可能较长，内容不会被截断。
            </p>
          </div>
        </div>
        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">课程</label>
            <select v-model="courseId" class="input" @change="onCourseChange">
              <option value="">未指定</option>
              <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
            </select>
          </div>
          <div>
            <label class="label">章节</label>
            <select v-model="chapterId" class="input" :disabled="!courseId" @change="onChapterChange">
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
                {{ l.lesson_no || `L${l.id}` }} {{ l.title || '' }}
              </option>
            </select>
          </div>
          <div>
            <label class="label">日期</label>
            <input v-model="date" type="date" class="input" />
          </div>
        </div>
      </div>

      <!-- 桌面端材料选择（方案 13.5）：注入已解析材料 chunk 作为检索候选 -->
      <div v-if="parsedMaterials.length" class="mt-3">
        <label class="label">关联课程材料（可选，{{ selectedMaterialIds.length }}/{{ parsedMaterials.length }} 已选）</label>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="m in parsedMaterials.slice(0, 24)"
            :key="m.id"
            type="button"
            class="rounded-full border px-3 py-1 text-xs transition"
            :class="selectedMaterialIds.includes(m.id)
              ? 'border-blue-500 bg-blue-600/20 text-[var(--acc-blue)]'
              : 'border-slate-700 bg-slate-900 text-slate-400 hover:border-slate-500'"
            :title="m.display_name || m.name || `材料 #${m.id}`"
            @click="toggleMaterial(m.id)"
          >
            #{{ m.id }} {{ (m.display_name || m.name || '').slice(0, 18) || '未命名' }}
          </button>
        </div>
      </div>

      <p v-if="error" class="mt-2 text-xs text-[var(--acc-red)]">{{ error }}</p>
    </section>

    <States :loading="running" :error="error" :empty="!runDetail && !nodes.length" empty-icon="headphones" empty-title="还没有运行结果">
      <section v-if="runDetail" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">DAG 节点流程</h3>
            <p class="card-muted">
              Run #{{ runDetail.id }} · 模式：{{ runDetail.mode }}
              <span v-if="runDetail.status === 'running'">· 实时刷新中</span>
            </p>
            <p class="mt-1 flex flex-wrap items-center gap-1.5">
              <span class="chip">{{ coverageView?.engine?.engine_version || domainView?.engine?.engine_version || 'v5' }}</span>
              <span v-if="coverageView?.engine?.mode === 'shadow'" class="chip chip-blue">shadow（不覆盖正式笔记）</span>
              <span v-else-if="coverageView?.engine?.mode === 'on'" class="chip chip-amber">V6 主链</span>
              <span v-else class="chip">V5 兼容模式</span>
              <span v-if="gate" class="chip" :class="gate === 'passed' ? 'chip-green' : (gate === 'failed' ? 'chip-red' : 'chip-amber')">
                覆盖门禁：{{ statusLabel(gate) }}
              </span>
            </p>
          </div>
          <StatusBadge :status="runDetail.status">{{ statusLabel(runDetail.status) }}</StatusBadge>
        </div>
        <DagFlow :nodes="nodes" :loading="runDetail.status === 'running'" />
        <p v-if="runDetail.status === 'degraded'" class="mt-3 rounded-lg px-3 py-2 text-xs"
           style="background: var(--acc-orange-soft); color: var(--acc-orange)">
          运行已降级（未达覆盖门禁）：{{ degradedReason || '覆盖指标低于阈值' }}
        </p>
        <p v-else-if="runDetail.status === 'failed'" class="mt-3 rounded-lg bg-rose-950/40 px-3 py-2 text-xs text-[var(--acc-red)]">
          运行失败：{{ runDetail.error || '请展开失败节点查看详细原因。' }}
        </p>
      </section>

      <!-- V6 Phase 1：材料域 + 覆盖账本（最小展示，不重做视觉设计） -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">材料完整性（V6 Phase 1）</h3>
            <p class="card-muted">
              Material Domain → Source Map → Coverage Ledger → Coverage Auditor。
              任何材料或 source span 都必须被处理，或留下可审计的未使用原因。
            </p>
          </div>
          <button type="button" class="btn btn-secondary" :disabled="!currentRunId" @click="currentRunId && loadCoverage(currentRunId)">
            刷新覆盖
          </button>
        </div>

        <p v-if="coverageError" class="mb-3 rounded-lg px-3 py-2 text-xs" style="background: var(--surface-inset); color: var(--txt-3)">
          {{ coverageError }}
        </p>

        <template v-if="domainView || coverageView">
          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div class="card-soft">
              <p class="stat-label">材料总数</p>
              <p class="stat-value">{{ num(domainView?.counts?.total_items) }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">唯一材料</p>
              <p class="stat-value">{{ num(domainView?.counts?.unique_items) }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">Source spans 总数</p>
              <p class="stat-value">{{ num(coverageMetrics?.total_source_spans) }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">已记账 span</p>
              <p class="stat-value">
                {{ num(coverageMetrics?.total_source_spans != null && coverageMetrics?.silent_dropped != null
                  ? Number(coverageMetrics.total_source_spans) - Number(coverageMetrics.silent_dropped) : null) }}
              </p>
            </div>
          </div>

          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mt-3">
            <div class="card-soft">
              <p class="stat-label">规范 span（非噪声）</p>
              <p class="stat-value">{{ num(coverageMetrics?.canonical_non_noise_spans) }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">实际语义处理</p>
              <p class="stat-value">{{ num(coverageMetrics?.processed_spans) }}</p>
              <p class="card-muted">
                语义处理率 {{ pct(coverageMetrics?.semantic_processing_rate) }}
                <span v-if="coverageMetrics?.semantic_rate_floor != null">
                  （Phase 1 下限 {{ pct(coverageMetrics?.semantic_rate_floor) }}）
                </span>
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">silent drop</p>
              <p class="stat-value"
                 :style="{ color: Number(coverageMetrics?.silent_dropped || 0) > 0 ? 'var(--acc-red)' : undefined }">
                {{ num(coverageMetrics?.silent_dropped) }}
              </p>
              <p class="card-muted">无账目且无原因的 canonical 非噪声 span</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">域记账率</p>
              <p class="stat-value">{{ pct(coverageMetrics?.domain_accounting_rate) }}</p>
            </div>
          </div>

          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mt-3">
            <div class="card-soft">
              <p class="stat-label">转写时间轴覆盖</p>
              <p class="stat-value">{{ pct(coverageMetrics?.timeline_coverage_rate) }}</p>
              <p class="card-muted">
                {{ num(coverageMetrics?.timeline_span_processed) }} / {{ num(coverageMetrics?.timeline_span_total) }} 段 ·
                {{ msToClock(coverageMetrics?.timeline_start_ms) }}–{{ msToClock(coverageMetrics?.timeline_end_ms) }}
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">PPT 页覆盖</p>
              <p class="stat-value">
                {{ num(coverageMetrics?.ppt_pages_processed) }} / {{ num(coverageMetrics?.ppt_pages_total) }}
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">Source refs 有效</p>
              <p class="stat-value">
                {{ num(coverageMetrics?.source_refs_valid) }} / {{ num(coverageMetrics?.source_refs_total) }}
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">重复 / 噪声 / 不支持</p>
              <p class="stat-value">
                {{ num(coverageMetrics?.duplicate_spans) }} / {{ num(coverageMetrics?.noise_spans) }} / {{ num(coverageMetrics?.unsupported_spans) }}
              </p>
            </div>
          </div>

          <p v-if="coverageView?.plan" class="card-muted mt-3">
            覆盖计划：{{ coverageView.plan.strategy }} · 计划 span {{ coverageView.plan.planned_span_count }} ·
            未分配 {{ coverageView.plan.unassigned_count }} · 输入预算 {{ coverageView.plan.input_budget_tokens }} tokens
            <span v-if="coverageView.plan.unassigned_count > 0" style="color: var(--acc-red)">（存在未分配 span，门禁不得通过）</span>
          </p>

          <!-- Phase 2：required 材料失败明细（必须显式可见，不得只显示一个 gate） -->
          <div v-if="requiredFailures.length" class="mt-4">
            <p class="stat-label" style="color: var(--acc-red)">必需材料问题（{{ requiredFailures.length }}）</p>
            <ul class="mt-1 text-xs" style="color: var(--acc-red)">
              <li v-for="(msg, i) in requiredFailures" :key="i">{{ msg }}</li>
            </ul>
          </div>

          <div v-if="coverageView?.unprocessed_reasons?.length" class="mt-4">
            <p class="stat-label">未处理原因列表（点击展开明细）</p>
            <div class="mt-2 flex flex-wrap gap-2">
              <button
                v-for="r in coverageView.unprocessed_reasons"
                :key="`${r.stage}-${r.reason_code}`"
                type="button"
                class="chip"
                @click="toggleReason(r.reason_code)"
              >
                {{ r.reason_code || '(no reason)' }} × {{ r.n }} · {{ r.stage }}
              </button>
            </div>
          </div>

          <div v-if="domainView?.items?.length" class="mt-4 overflow-x-auto">
            <p class="stat-label">Material Domain items</p>
            <table class="data-table mt-2">
              <thead>
                <tr>
                  <th>#</th><th>材料</th><th>来源</th><th>状态</th><th>原因</th><th>字符</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="it in domainView.items" :key="it.domain_item_id">
                  <td>{{ it.ordinal }}</td>
                  <td>{{ it.material_id != null ? `#${it.material_id}` : '内联转写' }}</td>
                  <td>{{ it.source_kind }}</td>
                  <td>
                    <StatusBadge :status="it.state">{{ statusLabel(it.state) }}</StatusBadge>
                    <span v-if="it.duplicate_of_item_id" class="card-muted"> → item {{ it.duplicate_of_item_id }}</span>
                  </td>
                  <td>
                    <span v-if="it.reason_code" class="text-xs">{{ it.reason_code }}</span>
                    <span v-else class="text-xs text-t3">—</span>
                    <p v-if="it.reason_detail" class="card-muted">{{ it.reason_detail }}</p>
                  </td>
                  <td>{{ it.raw_chars ?? '—' }}</td>
                  <td>
                    <div class="flex flex-wrap items-center gap-1">
                      <button
                        v-for="sid in (expandedItemId === it.domain_item_id
                          ? it.source_ids
                          : it.source_ids.slice(0, 3))"
                        :key="sid"
                        type="button"
                        class="btn btn-ghost !px-2 !py-1 text-xs"
                        :disabled="!currentRunId"
                        @click="openSpan(sid)"
                      >{{ sid }}</button>
                      <button
                        v-if="it.span_count > 3"
                        type="button"
                        class="btn btn-ghost !px-2 !py-1 text-xs"
                        @click="toggleItemSpans(it.domain_item_id)"
                      >{{ expandedItemId === it.domain_item_id ? '收起' : `+${it.span_count - 3}` }}</button>
                      <span v-if="!it.source_ids.length" class="card-muted">无 span</span>
                    </div>
                    <p v-if="it.span_count > it.source_ids.length" class="card-muted">
                      仅展示前 {{ it.source_ids.length }} 个 Source ID（共 {{ it.span_count }} 个）
                    </p>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="spanError" class="mt-2 text-xs" style="color: var(--acc-red)">{{ spanError }}</div>
          <div v-if="spanDetail" class="card-soft mt-3">
            <div class="flex items-start justify-between gap-2">
              <div>
                <p class="text-sm font-semibold text-t1">{{ spanDetail.source_id }}</p>
                <p class="card-muted">
                  {{ spanDetail.source_kind }} · {{ spanDetail.locator || '—' }} ·
                  {{ spanDetail.char_count ?? '—' }} 字符 / {{ spanDetail.token_count ?? '—' }} tokens
                  <span v-if="spanDetail.page_no"> · 第 {{ spanDetail.page_no }} 页</span>
                  <span v-if="spanDetail.slide_no"> · 第 {{ spanDetail.slide_no }} 张</span>
                  <span v-if="spanDetail.canonical_source_id"> · canonical {{ spanDetail.canonical_source_id }}</span>
                </p>
              </div>
              <div class="flex items-center gap-2">
                <StatusBadge :status="spanDetail.span_state">{{ statusLabel(spanDetail.span_state) }}</StatusBadge>
                <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs" @click="closeSpan">关闭</button>
              </div>
            </div>
            <pre class="code-panel mt-2 max-h-56 whitespace-pre-wrap">{{ truncate(spanDetail.text || '(空)', 2000) }}</pre>
          </div>

          <!-- 覆盖账本：每个 span 在每个阶段的终态（诚实显示旧生成链实际用量） -->
          <div v-if="ledgerRows.length" class="mt-4">
            <p class="stat-label">Coverage Ledger 账目汇总</p>
            <table class="data-table mt-2">
              <thead>
                <tr><th>阶段</th><th>结果</th><th>原因</th><th>数量</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in ledgerRows" :key="row.key">
                  <td>{{ row.stage }}</td>
                  <td><StatusBadge :status="row.outcome">{{ statusLabel(row.outcome) }}</StatusBadge></td>
                  <td>{{ row.reason || '—' }}</td>
                  <td>{{ row.n }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="card-muted">
          等待 resolve_material_domain / build_source_map / plan_coverage 节点完成。
        </p>
      </section>

      <!-- V6 Phase 2：全量分段理解审计 -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">全量课堂理解（V6 Phase 2）</h3>
            <p class="card-muted">
              segment_lesson → understand_segments → merge_lesson_understanding。
              全部 canonical 非噪声 span 必须恰好进入一个 primary segment，并在合并结果中被消费。
            </p>
          </div>
          <button type="button" class="btn btn-secondary" :disabled="!currentRunId"
                  @click="currentRunId && loadCoverage(currentRunId)">刷新理解</button>
        </div>

        <template v-if="segmentsView">
          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div class="card-soft">
              <p class="stat-label">segment 数量</p>
              <p class="stat-value">{{ num(segmentsView.segment_count) }}</p>
              <p class="card-muted">策略：{{ segmentsView.strategy || '—' }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">理解成功的 segment</p>
              <p class="stat-value">
                {{ segments.filter(s => s.status === 'succeeded').length }} / {{ num(segmentsView.segment_count) }}
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">merge 消费段数</p>
              <p class="stat-value">
                {{ num(lessonUnderstanding?.consumed_segment_count) }} / {{ num(lessonUnderstanding?.segment_count) }}
              </p>
              <p class="card-muted">必须消费全部 primary segment</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">LessonUnderstanding</p>
              <p class="stat-value">{{ lessonUnderstanding ? statusLabel(lessonUnderstanding.status) : '—' }}</p>
              <p class="card-muted">知识点 {{ lessonKnowledgeUnits.length }} · Source ID {{ num(lessonUnderstanding?.valid_source_count) }}</p>
            </div>
          </div>

          <div v-if="lessonConflicts.length" class="mt-3">
            <p class="stat-label" style="color: var(--acc-orange)">未解决冲突（必须人工确认）</p>
            <ul class="mt-1 text-xs" style="color: var(--acc-orange)">
              <li v-for="(c, i) in lessonConflicts" :key="i">{{ c }}</li>
            </ul>
          </div>

          <div v-if="segments.length" class="mt-4 overflow-x-auto">
            <p class="stat-label">segments（按课堂顺序）</p>
            <table class="data-table mt-2">
              <thead>
                <tr>
                  <th>#</th><th>状态</th><th>primary spans</th><th>Source ID 范围</th>
                  <th>模型</th><th>重试</th><th>耗时</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="seg in segments" :key="seg.segment_id">
                  <tr>
                    <td>{{ seg.ordinal }}</td>
                    <td><StatusBadge :status="seg.status">{{ statusLabel(seg.status) }}</StatusBadge></td>
                    <td>
                      {{ seg.primary_span_count }}
                      <span v-if="seg.overlap_span_count" class="card-muted">(+{{ seg.overlap_span_count }} overlap)</span>
                    </td>
                    <td class="text-xs">
                      {{ seg.primary_source_ids[0] || '—' }}
                      <span v-if="seg.primary_source_ids.length > 1">
                        … {{ seg.primary_source_ids[seg.primary_source_ids.length - 1] }}
                      </span>
                    </td>
                    <td class="text-xs">{{ seg.model_used || '—' }}</td>
                    <td>{{ seg.attempts }}</td>
                    <td>{{ fmtDuration(seg.duration_ms) }}</td>
                    <td>
                      <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs"
                              @click="toggleSegment(seg.segment_id)">
                        {{ expandedSegmentId === seg.segment_id ? '收起' : '查看理解' }}
                      </button>
                    </td>
                  </tr>
                  <tr v-if="seg.error">
                    <td colspan="8" class="text-xs" style="color: var(--acc-red)">{{ seg.error }}</td>
                  </tr>
                  <tr v-if="expandedSegmentId === seg.segment_id">
                    <td colspan="8">
                      <p class="card-muted">
                        input_hash {{ seg.input_hash }}
                        <template v-if="segmentUnderstanding(seg.segment_id)">
                          · prompt {{ segmentUnderstanding(seg.segment_id)?.prompt_version }}
                          · model {{ segmentUnderstanding(seg.segment_id)?.model_used }}
                          · source_refs {{ segmentUnderstanding(seg.segment_id)?.source_ref_count }}
                        </template>
                      </p>
                      <p class="stat-label mt-2">全部 Source ID（{{ seg.primary_source_ids.length }}）</p>
                      <p class="flex flex-wrap gap-1">
                        <span v-for="sid in seg.primary_source_ids" :key="sid" class="chip">{{ sid }}</span>
                      </p>
                      <template v-if="segmentUnderstanding(seg.segment_id)">
                        <p class="stat-label mt-3">本段知识点</p>
                        <ul class="mt-1 text-xs">
                          <li v-for="(ku, i) in (segmentUnderstanding(seg.segment_id)?.understanding?.knowledge_units || [])" :key="i">
                            <b>{{ ku.topic || ku.temp_id }}</b>：{{ ku.summary }}
                            <span class="card-muted">[{{ (ku.source_refs || []).join(', ') }}]</span>
                          </li>
                        </ul>
                        <p v-if="(segmentUnderstanding(seg.segment_id)?.understanding?.unresolved_points || []).length"
                           class="stat-label mt-2">本段未讲清</p>
                        <ul class="text-xs" style="color: var(--acc-orange)">
                          <li v-for="(p, i) in (segmentUnderstanding(seg.segment_id)?.understanding?.unresolved_points || [])" :key="i">{{ p }}</li>
                        </ul>
                      </template>
                      <p v-else class="card-muted">该 segment 尚无理解结果。</p>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>

          <div v-if="lessonKnowledgeUnits.length" class="mt-4">
            <p class="stat-label">LessonUnderstanding 知识点（{{ lessonKnowledgeUnits.length }}）</p>
            <table class="data-table mt-2">
              <thead><tr><th>#</th><th>主题</th><th>类型</th><th>概括</th><th>Source ID</th><th>来自段</th></tr></thead>
              <tbody>
                <tr v-for="(ku, i) in lessonKnowledgeUnits" :key="ku.id || i">
                  <td>{{ i + 1 }}</td>
                  <td>{{ ku.topic || '—' }}</td>
                  <td>{{ ku.kind || '—' }}</td>
                  <td>{{ truncate(ku.summary || '', 160) }}</td>
                  <td class="text-xs">{{ (ku.source_refs || []).join(', ') || '—' }}</td>
                  <td class="text-xs">{{ (ku.segment_ids || []).join(', ') || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="card-muted">
          等待 segment_lesson / understand_segments / merge_understanding 节点完成。
        </p>
      </section>

      <!-- V6 Phase 3：认知分析审计（不进入 HTML/PDF，仅供审阅） -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">认知分析（V6 Phase 3 · 审计）</h3>
            <p class="card-muted">
              student_simulator 分析学生可能如何理解、混淆或记忆已有课堂内容。
              仅用于审计，**不进入最终笔记**（Composer V2 尚未实施）。
            </p>
          </div>
          <button type="button" class="btn btn-secondary" :disabled="!currentRunId"
                  @click="currentRunId && loadCoverage(currentRunId)">刷新认知</button>
        </div>

        <template v-if="cognitiveView && cognitiveMap">
          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div class="card-soft">
              <p class="stat-label">CognitiveMap 状态</p>
              <p class="stat-value">{{ statusLabel(cognitiveMap.status) }}</p>
              <p class="card-muted">{{ cognitiveMap.model_used || '—' }}</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">输入 KU 覆盖</p>
              <p class="stat-value">{{ pct(cognitiveMap.cognitive_input_coverage) }}</p>
              <p class="card-muted">
                {{ num(cognitiveMap.knowledge_unit_processed) }} / {{ num(cognitiveMap.knowledge_unit_total) }} KU ·
                {{ num(cognitiveMap.batch_count) }} 批
              </p>
            </div>
            <div class="card-soft">
              <p class="stat-label">认知项总数</p>
              <p class="stat-value">{{ num(cognitiveItems.length) }}</p>
              <p class="card-muted">八类分组见下表</p>
            </div>
            <div class="card-soft">
              <p class="stat-label">是否进入笔记</p>
              <p class="stat-value">{{ cognitiveView.publication.included_in_document ? '是' : '否' }}</p>
              <p class="card-muted">{{ cognitiveView.publication.reason }}</p>
            </div>
          </div>

          <p v-if="cognitiveMap.error" class="mt-2 text-xs" style="color: var(--acc-red)">
            {{ cognitiveMap.error }}
          </p>

          <!-- 空结果必须明确说明，而不是显示空白 -->
          <p v-if="!cognitiveItems.length"
             class="mt-3 rounded-lg px-3 py-2 text-xs"
             style="background: var(--surface-inset); color: var(--txt-2)">
            {{ cognitiveMap.note || '未识别到需要额外认知加工的内容' }}
            （status={{ statusLabel(cognitiveMap.status) }}，覆盖率 {{ pct(cognitiveMap.cognitive_input_coverage) }}）
          </p>

          <div v-for="group in cognitiveGrouped" :key="group.type" class="mt-4">
            <p class="stat-label">
              {{ group.label }}（{{ group.items.length }}）
              <span class="card-muted">· {{ group.type }}</span>
            </p>
            <table class="data-table mt-2">
              <thead>
                <tr><th>标题</th><th>严重度</th><th>置信度</th><th>来源类型</th>
                    <th>Source ID</th><th>Knowledge Unit</th><th>建议处理</th></tr>
              </thead>
              <tbody>
                <tr v-for="item in group.items" :key="item.item_id">
                  <td>
                    <b>{{ item.title || '—' }}</b>
                    <p class="card-muted">{{ item.explanation }}</p>
                  </td>
                  <td>{{ pct(item.severity) }}</td>
                  <td>{{ pct(item.confidence) }}</td>
                  <td>
                    <span class="chip"
                          :class="item.origin === 'model_cognitive_inference' ? 'chip-amber' : 'chip-green'">
                      {{ originLabel(item.origin) }}
                    </span>
                  </td>
                  <td>
                    <button v-for="sid in item.source_refs" :key="sid" type="button"
                            class="btn btn-ghost !px-2 !py-1 text-xs"
                            :disabled="!currentRunId" @click="openSpan(sid)">{{ sid }}</button>
                    <span v-if="!item.source_refs.length" class="card-muted">—</span>
                  </td>
                  <td class="text-xs">{{ item.knowledge_unit_refs.join(', ') || '—' }}</td>
                  <td class="text-xs">{{ item.recommended_treatment }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p class="card-muted mt-3">
            输入可用性：
            <span v-for="(v, k) in cognitiveMap.availability" :key="k" class="chip mr-1">
              {{ k }}={{ v }}
            </span>
          </p>
        </template>
        <p v-else class="card-muted">
          该运行暂无 CognitiveMap（Phase 3 未执行，或 V6_LEARNING_ENGINE=off）。
        </p>
      </section>

      <section v-if="evidenceView || evidenceError" class="section">
        <div v-if="evidenceError" class="section-head">
          <p class="text-xs" style="color: var(--acc-red)">{{ evidenceError }}</p>
        </div>
        <EvidenceV2Panel :view="evidenceView" :busy="evidenceBusy"
                         :label="evidenceView?.ai_explanation_label || '模型教学补充（非课堂原话）'"
                         :span-text="evidenceSpanText"
                         @refresh="loadEvidence(currentRunId)"
                         @open-span="openSpan" />
      </section>

      <section v-if="qualityView" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">V6 质量与发布状态</h3>
            <p class="card-muted">覆盖、证据、审查、发布和同步相互独立；同步结果不会覆盖内容可信状态。</p>
          </div>
          <button type="button" class="btn btn-secondary" :disabled="!currentRunId"
                  @click="currentRunId && loadCoverage(currentRunId)">刷新状态</button>
        </div>
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <div v-for="item in [
                 ['处理', qualityView.quality.processing_status],
                 ['覆盖', qualityView.quality.coverage_status],
                 ['证据', qualityView.quality.evidence_status],
                 ['审查', qualityView.quality.review_status],
                 ['发布', qualityView.quality.publication_status],
                 ['同步', qualityView.quality.sync_status]
               ]" :key="item[0]" class="card-soft">
            <p class="stat-label">{{ item[0] }}</p>
            <p class="mt-2 text-sm font-semibold">{{ statusLabel(item[1]) }}</p>
          </div>
        </div>
        <p v-if="qualityView.quality.degradation_reason" class="mt-3 rounded-lg px-3 py-2 text-xs"
           style="background: color-mix(in srgb, var(--acc-red) 10%, transparent); color: var(--acc-red)">
          降级原因：{{ qualityView.quality.degradation_reason }}
        </p>
        <p v-if="qualityView.revisions.length" class="card-muted mt-3">
          已保存修订 {{ qualityView.revisions.length }} 个 · 当前内容哈希
          <code>{{ qualityView.revisions[qualityView.revisions.length - 1]?.content_hash }}</code>
        </p>
      </section>

      <section v-if="nodes.length" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">节点详情</h3>
            <p class="card-muted">点击节点展开错误信息、模型、token 等。</p>
          </div>
        </div>
        <NodeDetail :nodes="nodes" />
      </section>

      <section v-if="outputs" class="grid gap-4 xl:grid-cols-2">
        <div class="section">
          <div class="section-head">
            <div>
              <h3 class="card-title">课堂结构 / 学生视角</h3>
              <p class="card-muted">来自 student_simulator 节点。</p>
            </div>
          </div>
          <div v-if="outputKey('student_simulator')?.student_simulation">
            <p class="text-xs text-slate-400">主要主题：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-200">
              <li v-for="(t, i) in outputKey('student_simulator')?.student_simulation?.teacher_structure || []" :key="i">
                {{ t.topic || JSON.stringify(t) }}
              </li>
            </ul>
            <p class="mt-3 text-xs text-slate-400">学生问题：</p>
            <ul class="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-200">
              <li v-for="(q, i) in outputKey('student_simulator')?.student_simulation?.student_questions || []" :key="i">
                {{ q }}
              </li>
            </ul>
          </div>
        </div>
        <div class="section">
          <div class="section-head">
            <div>
              <h3 class="card-title">笔记预览</h3>
              <p class="card-muted">note_writer 输出；包含证据指针。</p>
            </div>
            <StatusBadge v-if="noteData" :status="noteData.fallback ? 'fallback' : 'generated'"
                         :variant="noteData?.fallback ? 'amber' : 'green'">
              {{ noteData?.fallback ? '占位' : '已生成' }}
            </StatusBadge>
          </div>
          <div v-if="noteData?.note" class="space-y-2">
            <p class="text-sm font-semibold text-slate-100">{{ noteData.note.title || '（无标题）' }}</p>
            <pre class="code-panel max-h-72 whitespace-pre-wrap">{{ truncate(noteData.note.body || '(空笔记)', 4000) }}</pre>
            <p v-if="publishPath" class="text-[11px] text-[var(--acc-green)]">已发布到：{{ publishPath }}</p>
            <div v-if="artifact" class="flex flex-wrap gap-2 pt-2">
              <a class="btn btn-primary" :href="ArtifactsApi.previewUrl(artifact.id)" target="_blank" rel="noopener">预览排版</a>
              <a class="btn btn-secondary" :href="ArtifactsApi.downloadUrl(artifact.id, 'html')">下载 HTML</a>
              <a v-if="artifact.has_pdf" class="btn btn-secondary" :href="ArtifactsApi.downloadUrl(artifact.id, 'pdf')">下载 PDF</a>
            </div>
          </div>
          <p v-else class="text-xs text-slate-500">等待 note_writer 节点完成。</p>
        </div>
      </section>
    </States>
  </div>
</template>
