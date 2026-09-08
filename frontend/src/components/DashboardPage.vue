<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChaptersApi, CoursesApi, ErrorsApi, ExamsApi, LessonsApi, RunsApi, UsageApi } from '../api/endpoints'
import type { Chapter, Course, ErrorItem, ExamEvent, Lesson, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatCard from './widgets/StatCard.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import ProgressBar from './widgets/ProgressBar.vue'
import { fmtRelative, parseUsageWindows, statusLabel } from '../utils/format'

const loading = ref(true)
const error = ref('')

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])
const runs = ref<WorkflowRun[]>([])
const errors = ref<ErrorItem[]>([])
const exams = ref<ExamEvent[]>([])
const usage = ref<any>(null)
const usageError = ref('')

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const results = await Promise.allSettled([
      CoursesApi.list(),
      ChaptersApi.list(),
      LessonsApi.list(),
      RunsApi.list(50),
      ErrorsApi.list(),
      ExamsApi.list(),
    ])
    if (results[0].status === 'fulfilled') courses.value = results[0].value
    if (results[1].status === 'fulfilled') chapters.value = results[1].value
    if (results[2].status === 'fulfilled') lessons.value = results[2].value
    if (results[3].status === 'fulfilled') runs.value = results[3].value
    if (results[4].status === 'fulfilled') errors.value = results[4].value
    if (results[5].status === 'fulfilled') exams.value = results[5].value
    const failed = results.find(r => r.status === 'rejected') as PromiseRejectedResult | undefined
    if (failed) error.value = String(failed.reason?.message || failed.reason || '加载失败')
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function loadUsage() {
  usageError.value = ''
  try {
    usage.value = await UsageApi.get()
  } catch (e: any) {
    usage.value = null
    usageError.value = e?.message || '网关用量接口不可用（不影响本地功能）'
  }
}

onMounted(() => {
  loadAll()
  loadUsage()
})

const stats = computed(() => [
  { key: 'courses', label: '课程', value: courses.value.length, emoji: '📚', hint: '本学期在跑课程' },
  { key: 'chapters', label: '章节', value: chapters.value.length, emoji: '🗂️', hint: '已录入章节总数' },
  { key: 'lessons', label: '课时', value: lessons.value.length, emoji: '🎓', hint: '已录入课时总数' },
  { key: 'runs', label: '运行任务', value: runs.value.length, emoji: '⚙️', hint: '近 50 次工作流' },
  { key: 'errors', label: '错题', value: errors.value.length, emoji: '❌', hint: '全部状态错题' },
])

const provisionalErrors = computed(() => errors.value.filter(e => (e.status || '').toLowerCase() === 'provisional').length)

const upcomingExams = computed(() => {
  const today = new Date().toISOString().slice(0, 10)
  return exams.value
    .filter(e => e.date && e.date >= today)
    .sort((a, b) => (a.date! < b.date! ? -1 : 1))
    .slice(0, 5)
})

const recentRuns = computed(() => runs.value.slice(0, 6))

function courseName(id?: number | null): string {
  if (!id) return '未指定课程'
  return courses.value.find(c => c.id === id)?.name || `课程 #${id}`
}

// 用量数据:支持新版 quotas[].windows[] 与旧版扁平字段
const quota = computed(() => {
  const parsed = parseUsageWindows(usage.value)
  const wins = parsed.windows
  const w5 = wins[0] || { used: 0, limit: 0, pct: null, label: '5 小时' }
  const w7 = wins[1] || { used: 0, limit: 0, pct: null, label: '7 天' }
  const w30 = wins[2] || { used: 0, limit: 0, pct: null, label: '30 天' }
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
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="🎯"
      title="总览"
      subtitle="围绕课程章节、听课 / 作业 / 错题 / 复习流水线的本地学习 Agent Runtime 控制台。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="loadAll">刷新数据</button>
        <button class="btn btn-secondary" @click="loadUsage">刷新额度</button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error" @click="loadAll">
      <section class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <StatCard
          v-for="s in stats"
          :key="s.key"
          :label="s.label"
          :value="s.value"
          :emoji="s.emoji"
          :hint="s.hint"
        />
      </section>
    </States>

    <section class="grid gap-4 xl:grid-cols-3">
      <!-- 即将到来的考试 -->
      <div class="section xl:col-span-2">
        <div class="section-head">
          <div>
            <h3 class="card-title">即将到来的考试</h3>
            <p class="card-muted">按日期升序排序，可在「课程与考试」中调整或补录。</p>
          </div>
          <span class="chip">{{ upcomingExams.length }} 场</span>
        </div>
        <States
          :loading="loading"
          :error="error"
          :empty="!upcomingExams.length"
          empty-icon="📅"
          empty-title="暂无即将到来的考试"
          empty-hint="前往「课程与考试」同步校历或手动添加。"
        >
          <div class="space-y-2">
            <div
              v-for="e in upcomingExams"
              :key="e.id"
              class="card-soft flex items-center justify-between gap-3"
            >
              <div class="min-w-0">
                <p class="truncate text-sm font-semibold text-slate-100">{{ e.title }}</p>
                <p class="truncate text-xs text-slate-500">
                  {{ e.course_name || courseName(e.course_id) }}
                </p>
              </div>
              <div class="flex items-center gap-2">
                <StatusBadge v-if="e.event_type" :status="e.event_type" variant="blue">
                  {{ statusLabel(e.event_type) }}
                </StatusBadge>
                <span class="chip">{{ e.date }}</span>
              </div>
            </div>
          </div>
        </States>
      </div>

      <!-- 网关额度 -->
      <div class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">微信免费 DeepSeek 额度</h3>
            <p class="card-muted">数据来自 8080 网关用量接口（仅展示，不写入本地）。</p>
          </div>
          <StatusBadge
            :status="quota.available ? 'available' : 'unavailable'"
            :variant="quota.available ? 'green' : 'amber'"
          >
            {{ quota.available ? '已连接' : '未连接' }}
          </StatusBadge>
        </div>
        <div class="space-y-3">
          <div>
            <ProgressBar :percent="quota.pct5" label="5 小时窗口" />
            <p class="mt-1 text-[11px] text-slate-500">
              已用 {{ quota.used5 }} / {{ quota.limit5 }} 次请求
            </p>
          </div>
          <div>
            <ProgressBar :percent="quota.pct7" :warn-at="80" label="7 天窗口" />
            <p class="mt-1 text-[11px] text-slate-500">
              已用 {{ quota.used7 }} / {{ quota.limit7 }}
            </p>
          </div>
          <div>
            <ProgressBar :percent="quota.pct30" :warn-at="80" label="30 天窗口" />
            <p class="mt-1 text-[11px] text-slate-500">
              已用 {{ quota.used30 }} / {{ quota.limit30 }}
            </p>
          </div>
        </div>
        <p v-if="usageError" class="mt-3 text-[11px] text-amber-300">{{ usageError }}</p>
      </div>
    </section>

    <section class="grid gap-4 xl:grid-cols-3">
      <div class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">错题分布</h3>
            <p class="card-muted">Provisional / Confirmed / Rejected 全状态计数。</p>
          </div>
        </div>
        <States :loading="loading" :error="error" :empty="!errors.length" empty-icon="🎯" empty-title="暂无错题">
          <div class="grid grid-cols-3 gap-2 text-center">
            <div class="card-soft !p-3">
              <p class="text-xs text-slate-400">待确认</p>
              <p class="text-xl font-bold text-purple-300">{{ provisionalErrors }}</p>
            </div>
            <div class="card-soft !p-3">
              <p class="text-xs text-slate-400">已确认</p>
              <p class="text-xl font-bold text-emerald-300">
                {{ errors.filter(e => (e.status || '').toLowerCase() === 'confirmed').length }}
              </p>
            </div>
            <div class="card-soft !p-3">
              <p class="text-xs text-slate-400">已拒绝</p>
              <p class="text-xl font-bold text-rose-300">
                {{ errors.filter(e => (e.status || '').toLowerCase() === 'rejected').length }}
              </p>
            </div>
          </div>
        </States>
      </div>

      <div class="section xl:col-span-2">
        <div class="section-head">
          <div>
            <h3 class="card-title">最近运行</h3>
            <p class="card-muted">展示最近 6 次工作流运行，点进「运行日志」可看节点详情。</p>
          </div>
          <span class="chip">{{ runs.length }} 次</span>
        </div>
        <States :loading="loading" :error="error" :empty="!recentRuns.length" empty-icon="📜" empty-title="暂无运行记录">
          <div class="space-y-2">
            <div
              v-for="r in recentRuns"
              :key="r.id"
              class="card-soft flex items-center justify-between gap-3"
            >
              <div class="min-w-0">
                <p class="truncate text-sm font-semibold text-slate-100">{{ r.workflow }} · #{{ r.id }}</p>
                <p class="truncate text-xs text-slate-500">
                  {{ r.course_id ? courseName(r.course_id) : '未关联课程' }}
                </p>
              </div>
              <div class="flex items-center gap-2">
                <StatusBadge :status="r.status">{{ statusLabel(r.status) }}</StatusBadge>
                <span class="text-[11px] text-slate-500">{{ fmtRelative(r.created_at) }}</span>
              </div>
            </div>
          </div>
        </States>
      </div>
    </section>
  </div>
</template>
