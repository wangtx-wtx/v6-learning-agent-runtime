<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ChaptersApi, CoursesApi, LessonWorkflowApi, LessonsApi, RunsApi } from '../api/endpoints'
import type { Chapter, Course, Lesson, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import { toDagNodes } from './widgets/dag-types'
import { safeJson, truncate } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])

const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const lessonId = ref<number | ''>('')
const transcript = ref('')
const date = ref(new Date().toISOString().slice(0, 10))

const running = ref(false)
const error = ref('')
const currentRunId = ref<number | null>(null)
const nodes = ref<ReturnType<typeof toDagNodes>>([])
const runDetail = ref<WorkflowRun | null>(null)
const outputs = ref<Record<string, any> | null>(null)
const pollTimer = ref<number | null>(null)
const toast = useToast()

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

async function runFlow() {
  error.value = ''
  if (!transcript.value.trim()) {
    error.value = '请粘贴课堂转写文本。'
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
    })
    currentRunId.value = r.run_id
    await fetchRun(r.run_id)
    startPolling(r.run_id)
    toast.success(`已启动听课流 #${r.run_id}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    toast.error(`运行失败:${e?.message || e}`)
  } finally {
    running.value = false
  }
}

async function fetchRun(id: number) {
  try {
    const r = await LessonWorkflowApi.get(id)
    nodes.value = toDagNodes((r.nodes || []) as any)
    runDetail.value = r.run
    if (r.run?.output_json) {
      outputs.value = safeJson(r.run.output_json, null) || outputs.value
    }
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

function startPolling(id: number) {
  stopPolling()
  pollTimer.value = window.setInterval(async () => {
    await fetchRun(id)
    if (runDetail.value && runDetail.value.status !== 'running') {
      stopPolling()
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

function outputKey(key: string) {
  if (!outputs.value) return null
  return outputs.value[key] || null
}

onBeforeUnmount(() => {
  stopPolling()
})

onMounted(async () => {
  await loadMeta()
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
      emoji="📝"
      title="听课流"
      subtitle="粘贴课堂转写文本，运行本地多智能体流水线：转写切段 → 结构化 → 学生视角模拟 → 笔记写作 → 独立审查 → 证据审计 → 发布到 Obsidian。"
    >
      <template #actions>
        <button class="btn btn-secondary" :disabled="running || !currentRunId" @click="currentRunId && fetchRun(currentRunId)">
          刷新结果
        </button>
        <button class="btn btn-primary" :disabled="running" @click="runFlow">
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
      <p v-if="error" class="mt-2 text-xs text-rose-300">{{ error }}</p>
    </section>

    <States :loading="running" :error="error" :empty="!runDetail && !nodes.length" empty-icon="📝" empty-title="还没有运行结果">
      <section v-if="runDetail" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">DAG 节点流程</h3>
            <p class="card-muted">
              Run #{{ runDetail.id }} · 模式：{{ runDetail.mode }}
              <span v-if="runDetail.status === 'running'">· 实时刷新中</span>
            </p>
          </div>
          <StatusBadge :status="runDetail.status" />
        </div>
        <DagFlow :nodes="nodes" :loading="runDetail.status === 'running'" />
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
            <p v-if="publishPath" class="text-[11px] text-emerald-300">已发布到：{{ publishPath }}</p>
          </div>
          <p v-else class="text-xs text-slate-500">等待 note_writer 节点完成。</p>
        </div>
      </section>
    </States>
  </div>
</template>
