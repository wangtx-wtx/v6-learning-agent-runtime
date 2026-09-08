<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { CoursesApi, RunsApi } from '../api/endpoints'
import type { Course, RunNode, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import DagFlow from './widgets/DagFlow.vue'
import NodeDetail from './widgets/NodeDetail.vue'
import { toDagNodes } from './widgets/dag-types'
import { fmtRelative, statusLabel } from '../utils/format'
import { useRunEvents } from '../composables/useRunEvents'

const runs = ref<WorkflowRun[]>([])
const courses = ref<Course[]>([])
const loading = ref(false)
const error = ref('')
const selectedRun = ref<WorkflowRun | null>(null)
const selectedNodes = ref<ReturnType<typeof toDagNodes>>([])
const detailLoading = ref(false)
const detailError = ref('')
// SSE 实时订阅（useRunEvents 内置指数退避重连与轮询降级，方案 13.2）
const runEvents = useRunEvents()
const liveMode = ref<'sse' | 'polling' | 'off'>('off')

const filter = ref<string>('all')
const cancelState = ref<'idle' | 'pending' | 'done' | 'error'>('idle')
const cancelMsg = ref('')

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
  resetCancelState()
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
  // 运行中 → 订阅实时事件（SSE 优先，断线自动退避/降级轮询）
  if (['queued', 'running'].includes(r.status)) {
    runEvents.subscribe(r.id, (run: WorkflowRun, nodes: RunNode[]) => {
      selectedRun.value = run
      selectedNodes.value = toDagNodes((nodes || []) as any)
      liveMode.value = runEvents.mode.value
      if (['completed', 'failed', 'cancelled'].includes(run.status)) {
        void load()   // 刷新列表状态
      }
    })
    liveMode.value = runEvents.mode.value
  } else {
    runEvents.stop()
    liveMode.value = 'off'
  }
}

function courseName(id?: number | null) {
  if (!id) return '未关联课程'
  return courses.value.find(c => c.id === id)?.name || `课程 #${id}`
}

function closeDetail() {
  runEvents.stop()
  liveMode.value = 'off'
  selectedRun.value = null
  selectedNodes.value = []
}

const canCancel = computed(() =>
  ['queued', 'running'].includes(selectedRun.value?.status || ''),
)

async function cancelRun() {
  if (!selectedRun.value) return
  cancelState.value = 'pending'
  cancelMsg.value = ''
  try {
    await RunsApi.cancel(selectedRun.value.id)
    cancelState.value = 'done'
    cancelMsg.value = '已请求取消'
    const detail = await RunsApi.get(selectedRun.value.id)
    selectedRun.value = detail.run
    selectedNodes.value = toDagNodes((detail.nodes || []) as any)
  } catch (e: any) {
    cancelState.value = 'error'
    cancelMsg.value = e?.message || String(e)
  }
}

function resetCancelState() {
  cancelState.value = 'idle'
  cancelMsg.value = ''
}

onBeforeUnmount(() => {
  runEvents.stop()
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
                    <span
                      v-if="liveMode !== 'off'"
                      class="inline-flex items-center gap-1 rounded-full border border-blue-500/40 bg-blue-950/40 px-2 py-0.5 text-[10px] text-blue-300"
                      :title="liveMode === 'sse' ? 'SSE 实时推送已连接' : 'SSE 不可用，已降级为轮询'"
                    >{{ liveMode === 'sse' ? '实时' : '轮询中' }}</span>
                    <button
                      v-if="canCancel"
                      class="btn btn-ghost !px-2 !py-1 text-xs text-rose-300 disabled:opacity-50"
                      :disabled="cancelState === 'pending'"
                      @click="cancelRun"
                    >
                      {{ cancelState === 'pending' ? '正在取消…' : '取消运行' }}
                    </button>
                    <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="closeDetail">关闭</button>
                  </div>
                </div>
                <p v-if="cancelMsg" class="mb-2 rounded-lg px-3 py-2 text-xs"
                   :class="cancelState === 'error' ? 'bg-rose-950/40 text-rose-200' : 'bg-emerald-950/40 text-emerald-200'">
                  {{ cancelState === 'error' ? '取消失败：' : '' }}{{ cancelMsg }}
                </p>
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
