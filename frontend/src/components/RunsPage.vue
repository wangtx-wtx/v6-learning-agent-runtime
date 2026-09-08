<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CoursesApi, RunsApi } from '../api/endpoints'
import type { Course, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import { toDagNodes } from './widgets/dag-types'
import { fmtRelative, statusLabel } from '../utils/format'

const runs = ref<WorkflowRun[]>([])
const courses = ref<Course[]>([])
const loading = ref(false)
const error = ref('')
const selectedRun = ref<WorkflowRun | null>(null)
const selectedNodes = ref<ReturnType<typeof toDagNodes>>([])
const detailLoading = ref(false)
const detailError = ref('')

const filter = ref<string>('all')
const pollTimer = ref<number | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [r, c] = await Promise.all([RunsApi.list(100), CoursesApi.list()])
    runs.value = r || []
    courses.value = c || []
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

const filteredRuns = computed(() => {
  if (filter.value === 'all') return runs.value
  return runs.value.filter(r => r.workflow === filter.value)
})

const workflows = computed(() => {
  const set = new Set<string>()
  runs.value.forEach(r => set.add(r.workflow || 'unknown'))
  return Array.from(set)
})

async function selectRun(r: WorkflowRun) {
  selectedRun.value = r
  selectedNodes.value = []
  detailLoading.value = true
  detailError.value = ''
  try {
    const detail = await RunsApi.get(r.id)
    selectedRun.value = detail.run
    selectedNodes.value = toDagNodes((detail.nodes || []) as any)
  } catch (e: any) {
    detailError.value = e?.message || String(e)
  } finally {
    detailLoading.value = false
  }
}

function courseName(id?: number | null) {
  if (!id) return '未关联课程'
  return courses.value.find(c => c.id === id)?.name || `课程 #${id}`
}

function closeDetail() {
  selectedRun.value = null
  selectedNodes.value = []
}

function startPollingIfRunning() {
  stopPolling()
  if (!selectedRun.value) return
  if (selectedRun.value.status !== 'running') return
  pollTimer.value = window.setInterval(async () => {
    if (!selectedRun.value) return
    try {
      const detail = await RunsApi.get(selectedRun.value.id)
      selectedRun.value = detail.run
      selectedNodes.value = toDagNodes((detail.nodes || []) as any)
      if (detail.run.status !== 'running') stopPolling()
    } catch {
      stopPolling()
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

watch(selectedRun, () => startPollingIfRunning())

onBeforeUnmount(() => {
  stopPolling()
})

onMounted(async () => {
  await load()
  if (runs.value.length) {
    selectRun(runs.value[0])
  }
})
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="📜"
      title="运行日志"
      subtitle="展示最近 100 次工作流运行；点击卡片查看节点流程、状态、模型与错误。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="load">刷新列表</button>
        <select v-model="filter" class="input !w-48">
          <option value="all">全部工作流 ({{ runs.length }})</option>
          <option v-for="w in workflows" :key="w" :value="w">{{ w }}</option>
        </select>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error" :empty="!runs.length" empty-icon="📜" empty-title="还没有运行记录">
      <section class="grid gap-3 lg:grid-cols-[1fr_1.5fr]">
        <!-- 列表 -->
        <div class="space-y-2">
          <div
            v-for="r in filteredRuns"
            :key="r.id"
            class="card-soft cursor-pointer transition"
            :class="selectedRun?.id === r.id ? 'border-blue-500/60 bg-blue-950/30' : ''"
            @click="selectRun(r)"
          >
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <p class="truncate text-sm font-semibold text-slate-100">{{ r.workflow }} · #{{ r.id }}</p>
                <p class="truncate text-xs text-slate-500">{{ courseName(r.course_id) }}</p>
              </div>
              <StatusBadge :status="r.status">{{ statusLabel(r.status) }}</StatusBadge>
            </div>
            <p class="mt-1 text-[11px] text-slate-500">{{ fmtRelative(r.created_at) }}</p>
            <p v-if="r.error" class="mt-1 truncate text-[11px] text-rose-300" :title="r.error">{{ r.error }}</p>
          </div>
        </div>

        <!-- 详情 -->
        <div>
          <States
            :loading="detailLoading"
            :error="detailError"
            :empty="!selectedRun"
            empty-icon="📜"
            empty-title="选择左侧运行记录查看详情"
          >
            <div v-if="selectedRun" class="space-y-4">
              <div class="section">
                <div class="section-head">
                  <div>
                    <h3 class="card-title">{{ selectedRun.workflow }} · Run #{{ selectedRun.id }}</h3>
                    <p class="card-muted">
                      {{ courseName(selectedRun.course_id) }}
                      <span v-if="selectedRun.chapter_id"> · 章节 #{{ selectedRun.chapter_id }}</span>
                      <span v-if="selectedRun.lesson_id"> · 课时 #{{ selectedRun.lesson_id }}</span>
                      <span v-if="selectedRun.created_at"> · {{ fmtRelative(selectedRun.created_at) }}</span>
                    </p>
                  </div>
                  <div class="flex gap-2">
                    <StatusBadge :status="selectedRun.status">{{ statusLabel(selectedRun.status) }}</StatusBadge>
                    <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="closeDetail">关闭</button>
                  </div>
                </div>
                <DagFlow :nodes="selectedNodes" :loading="selectedRun.status === 'running'" />
                <p v-if="selectedRun.error" class="mt-3 rounded-lg bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
                  {{ selectedRun.error }}
                </p>
              </div>

              <div class="section">
                <div class="section-head">
                  <div>
                    <h3 class="card-title">节点详情</h3>
                    <p class="card-muted">点击节点查看模型 / 错误 / 输出。</p>
                  </div>
                </div>
                <NodeDetail :nodes="selectedNodes" />
              </div>
            </div>
          </States>
        </div>
      </section>
    </States>
  </div>
</template>
