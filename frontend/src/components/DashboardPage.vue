<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChaptersApi, CoursesApi, ErrorsApi, ExamsApi, LessonsApi, RunsApi, ScheduleApi } from '../api/endpoints'
import type { Chapter, Course, EffectiveSchedule, ErrorItem, ExamEvent, Lesson, WorkflowRun } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatCard from './widgets/StatCard.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import Icon from './widgets/Icon.vue'
import { fmtRelative, statusLabel } from '../utils/format'

const loading = ref(true)
const error = ref('')

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])
const runs = ref<WorkflowRun[]>([])
const errors = ref<ErrorItem[]>([])
const exams = ref<ExamEvent[]>([])
const schedule = ref<EffectiveSchedule | null>(null)
const scheduleError = ref('')

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
      ScheduleApi.effective({ days: 7 }),
    ])
    if (results[0].status === 'fulfilled') courses.value = results[0].value
    if (results[1].status === 'fulfilled') chapters.value = results[1].value
    if (results[2].status === 'fulfilled') lessons.value = results[2].value
    if (results[3].status === 'fulfilled') runs.value = results[3].value
    if (results[4].status === 'fulfilled') errors.value = results[4].value
    if (results[5].status === 'fulfilled') exams.value = results[5].value
    if (results[6].status === 'fulfilled') {
      schedule.value = results[6].value
      scheduleError.value = ''
    } else {
      schedule.value = null
      scheduleError.value = String(results[6].reason?.message || results[6].reason || '七日课表加载失败')
    }
    const failed = results.slice(0, 6).find(r => r.status === 'rejected') as PromiseRejectedResult | undefined
    if (failed) error.value = String(failed.reason?.message || failed.reason || '加载失败')
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadAll()
})

const stats = computed(() => [
  { key: 'courses', label: '课程', value: courses.value.length, icon: 'book-open', tone: 'blue' as const, hint: '本学期在跑课程' },
  { key: 'chapters', label: '章节', value: chapters.value.length, icon: 'layers', tone: 'violet' as const, hint: '已录入章节总数' },
  { key: 'lessons', label: '课时', value: lessons.value.length, icon: 'graduation', tone: 'emerald' as const, hint: '已录入课时总数' },
  { key: 'runs', label: '运行任务', value: runs.value.length, icon: 'activity', tone: 'amber' as const, hint: '近 50 次工作流' },
  { key: 'errors', label: '错题', value: errors.value.length, icon: 'circle-x', tone: 'rose' as const, hint: '全部状态错题' },
])

const provisionalErrors = computed(() => errors.value.filter(e => (e.status || '').toLowerCase() === 'provisional').length)
const confirmedErrors = computed(() => errors.value.filter(e => (e.status || '').toLowerCase() === 'confirmed').length)
const rejectedErrors = computed(() => errors.value.filter(e => (e.status || '').toLowerCase() === 'rejected').length)

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

/* —— 日期展示辅助 —— */
function dateMonth(d?: string | null): string {
  if (!d) return '--'
  return String(Number(d.slice(5, 7)) || '--')
}
function dateDay(d?: string | null): string {
  if (!d) return '--'
  return d.slice(8, 10)
}
function daysLeft(d?: string | null): number | null {
  if (!d) return null
  const target = new Date(`${d}T00:00:00`)
  if (Number.isNaN(target.getTime())) return null
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return Math.round((target.getTime() - today.getTime()) / 86400000)
}
function urgency(d?: string | null): string {
  const n = daysLeft(d)
  if (n === null) return 'calm'
  if (n <= 7) return 'hot'
  if (n <= 21) return 'soon'
  return 'calm'
}

const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

function weekdayLabel(value: number): string {
  return weekdays[value - 1] || `周${value}`
}

function monthDay(value: string): string {
  return `${Number(value.slice(5, 7))}月${Number(value.slice(8, 10))}日`
}

function periodLabel(periods?: number[]): string {
  if (!periods?.length) return '时间待定'
  return periods.length === 1 ? `第 ${periods[0]} 节` : `第 ${periods[0]}–${periods[periods.length - 1]} 节`
}

function courseStatusLabel(status: string): string {
  if (status === 'rescheduled') return '调课'
  if (status === 'makeup') return '补课'
  if (status === 'workday') return '补班课表'
  return ''
}
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="dashboard"
      title="总览"
      subtitle="围绕课程章节、听课 / 作业 / 错题 / 复习流水线的本地学习 Agent Runtime 控制台。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="loadAll">
          <Icon name="refresh" :size="15" />
          刷新数据
        </button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error">
      <section class="grid grid-cols-2 gap-3.5 md:grid-cols-3 xl:grid-cols-5">
        <StatCard
          v-for="s in stats"
          :key="s.key"
          :label="s.label"
          :value="s.value"
          :icon="s.icon"
          :tone="s.tone"
          :hint="s.hint"
        />
      </section>
    </States>

    <!-- 今天起连续七天的最终课表 -->
    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">未来七天课程</h3>
          <p class="card-muted">从今天开始，已合并固定课表、节假日、调休与补班安排。</p>
        </div>
        <RouterLink class="btn btn-secondary" to="/calendar">
          <Icon name="calendar" :size="15" />
          管理课表
        </RouterLink>
      </div>
      <States
        :loading="loading"
        :error="scheduleError"
        :empty="!schedule?.days?.length"
        empty-icon="calendar"
        empty-title="尚未生成七日课表"
        empty-hint="前往「课表与校历」设置学期与固定课程。"
      >
        <div class="week-strip">
          <article
            v-for="day in schedule?.days || []"
            :key="day.date"
            class="day-column"
            :class="{ 'is-today': day.is_today, 'is-off': day.is_day_off }"
          >
            <header class="day-head">
              <div>
                <p class="day-weekday">{{ weekdayLabel(day.weekday) }}</p>
                <p class="day-date">{{ monthDay(day.date) }}</p>
              </div>
              <span v-if="day.is_today" class="chip chip-blue">今天</span>
              <span v-else-if="day.week_number" class="day-week">第 {{ day.week_number }} 周</span>
            </header>
            <div v-if="day.events.length" class="space-y-1.5">
              <div v-for="event in day.events" :key="event.id" class="calendar-event">
                <strong>{{ event.title || (event.adjustment_type === 'workday' ? '补班' : '日程调整') }}</strong>
                <span v-if="event.adjustment_type === 'workday' && event.source_date">
                  补 {{ event.source_date }} 的课程
                </span>
                <span v-else-if="event.detail">{{ event.detail }}</span>
              </div>
            </div>
            <div v-if="day.courses.length" class="space-y-2">
              <div v-for="course in day.courses" :key="course.id" class="schedule-course" :class="`status-${course.status}`">
                <div class="flex items-start justify-between gap-2">
                  <p class="line-clamp-2 text-[12.5px] font-semibold leading-5 text-t1">{{ course.course_name }}</p>
                  <span v-if="courseStatusLabel(course.status)" class="schedule-tag">{{ courseStatusLabel(course.status) }}</span>
                </div>
                <p class="mt-1 tnum text-[11px] text-t2">
                  {{ periodLabel(course.periods) }}
                  <template v-if="course.start_time"> · {{ course.start_time }}{{ course.end_time ? `–${course.end_time}` : '' }}</template>
                </p>
                <p v-if="course.location" class="mt-1 truncate text-[11px] text-t3">{{ course.location }}</p>
                <p v-if="course.teacher" class="mt-0.5 truncate text-[10.5px] text-t3">{{ course.teacher }}</p>
              </div>
            </div>
            <div v-else class="day-empty">
              {{ day.is_day_off ? '今日放假 / 调休' : '今日无课' }}
            </div>
          </article>
        </div>
      </States>
    </section>

    <section class="grid gap-4 xl:grid-cols-3">
      <div class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">错题分布</h3>
            <p class="card-muted">Provisional / Confirmed / Rejected 全状态计数。</p>
          </div>
        </div>
        <States :loading="loading" :error="error" :empty="!errors.length" empty-icon="target" empty-title="暂无错题">
          <div class="grid grid-cols-3 gap-2.5">
            <div class="mini-stat tone-violet">
              <p class="mini-label">待确认</p>
              <p class="mini-value">{{ provisionalErrors }}</p>
            </div>
            <div class="mini-stat tone-emerald">
              <p class="mini-label">已确认</p>
              <p class="mini-value">{{ confirmedErrors }}</p>
            </div>
            <div class="mini-stat tone-rose">
              <p class="mini-label">已拒绝</p>
              <p class="mini-value">{{ rejectedErrors }}</p>
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
        <States :loading="loading" :error="error" :empty="!recentRuns.length" empty-icon="scroll" empty-title="暂无运行记录">
          <div class="space-y-2">
            <div
              v-for="r in recentRuns"
              :key="r.id"
              class="card-soft flex items-center justify-between gap-3"
            >
              <div class="flex min-w-0 items-center gap-3">
                <span class="run-glyph">
                  <Icon name="activity" :size="14" />
                </span>
                <div class="min-w-0">
                  <p class="truncate text-[13.5px] font-semibold text-t1">
                    {{ r.workflow }} <span class="tnum text-t3">#{{ r.id }}</span>
                  </p>
                  <p class="mt-0.5 truncate text-[12px] text-t3">
                    {{ r.course_id ? courseName(r.course_id) : '未关联课程' }}
                  </p>
                </div>
              </div>
              <div class="flex shrink-0 items-center gap-2.5">
                <StatusBadge :status="r.status">{{ statusLabel(r.status) }}</StatusBadge>
                <span class="tnum w-14 text-right text-[11.5px] text-t3">{{ fmtRelative(r.created_at) }}</span>
              </div>
            </div>
          </div>
        </States>
      </div>
    </section>

    <!-- 即将到来的考试：总览最底部 -->
    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">即将到来的考试</h3>
          <p class="card-muted">按日期升序显示最近五场，可在「课程与考试」中调整或补录。</p>
        </div>
        <div class="flex items-center gap-2">
          <span class="chip chip-blue">{{ upcomingExams.length }} 场</span>
          <RouterLink class="btn btn-secondary" to="/courses">管理考试</RouterLink>
        </div>
      </div>
      <States
        :loading="loading"
        :error="error"
        :empty="!upcomingExams.length"
        empty-icon="calendar"
        empty-title="暂无即将到来的考试"
        empty-hint="前往「课程与考试」手动添加。"
      >
        <div class="space-y-2">
          <div v-for="e in upcomingExams" :key="e.id" class="exam-row">
            <div class="flex min-w-0 items-center gap-3.5">
              <div class="date-block">
                <span class="date-m">{{ dateMonth(e.date) }}月</span>
                <span class="date-d">{{ dateDay(e.date) }}</span>
              </div>
              <div class="min-w-0">
                <p class="truncate text-[13.5px] font-semibold text-t1">{{ e.title }}</p>
                <p class="mt-0.5 truncate text-[12px] text-t3">{{ e.course_name || courseName(e.course_id) }}</p>
              </div>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <span class="countdown" :class="`urg-${urgency(e.date)}`">D-{{ daysLeft(e.date) }}</span>
              <StatusBadge v-if="e.event_type" :status="e.event_type" variant="blue">
                {{ statusLabel(e.event_type) }}
              </StatusBadge>
            </div>
          </div>
        </div>
      </States>
    </section>
  </div>
</template>

<style scoped>
/* 七日课表：宽屏七列，窄屏横向滚动，保持每天一列。 */
.week-strip {
  display: grid;
  grid-template-columns: repeat(7, minmax(150px, 1fr));
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 3px;
}
.day-column {
  min-height: 210px;
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: var(--r-ctl);
  background: var(--surface-inset);
}
.day-column.is-today {
  border-color: color-mix(in srgb, var(--accent) 52%, transparent);
  box-shadow: inset 0 2px 0 var(--accent);
}
.day-column.is-off { background: var(--acc-orange-soft); }
.day-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 7px;
  min-height: 45px;
  margin-bottom: 9px;
}
.day-weekday { font-size: 12.5px; font-weight: 650; color: var(--txt-1); }
.day-date { margin-top: 2px; font-size: 11px; color: var(--txt-3); }
.day-week { font-size: 10px; color: var(--txt-3); white-space: nowrap; }
.calendar-event {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 7px;
  padding: 7px 8px;
  border-radius: 8px;
  background: var(--acc-orange-soft);
  color: var(--acc-orange);
  font-size: 10.5px;
  line-height: 1.4;
}
.schedule-course {
  padding: 8px;
  border-left: 3px solid var(--accent);
  border-radius: 8px;
  background: var(--surface-1);
}
.schedule-course.status-rescheduled { border-left-color: var(--acc-orange); }
.schedule-course.status-makeup { border-left-color: var(--acc-green); }
.schedule-tag {
  flex-shrink: 0;
  padding: 2px 5px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 9px;
  font-weight: 650;
}
.day-empty {
  display: grid;
  min-height: 115px;
  place-items: center;
  text-align: center;
  color: var(--txt-3);
  font-size: 11px;
}

/* —— 考试行 —— */
.exam-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgb(var(--glass-rgb) / calc(var(--glass-a) * 0.42));
  transition: background 0.18s var(--ease), transform 0.18s var(--ease);
}
.exam-row:hover {
  background: rgb(var(--glass-rgb) / calc(var(--glass-a) * 0.66));
  transform: translateX(2px);
}

/* 日期块 */
.date-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  flex-shrink: 0;
  border-radius: 10px;
  background: var(--accent-soft);
}
.date-m {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--accent);
  line-height: 1;
}
.date-d {
  margin-top: 3px;
  font-size: 16px;
  font-weight: 600;
  line-height: 1;
  letter-spacing: -0.02em;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}

/* 倒计时 */
.countdown {
  padding: 3px 8px;
  border-radius: 7px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.01em;
}
.urg-calm { background: var(--acc-gray-soft); color: var(--acc-gray); }
.urg-soon { background: var(--acc-orange-soft); color: var(--acc-orange); }
.urg-hot { background: var(--acc-red-soft); color: var(--acc-red); }

/* —— 迷你统计块（错题分布）—— */
.mini-stat {
  padding: 12px 10px;
  border-radius: 10px;
  background: var(--tone-soft, var(--surface-inset));
  text-align: center;
  transition: transform 0.18s var(--ease);
}
.mini-stat:hover { transform: translateY(-1px); }
.mini-label { font-size: 11.5px; color: var(--txt-2); }
.mini-value {
  margin-top: 6px;
  font-size: 22px;
  font-weight: 600;
  line-height: 1;
  letter-spacing: -0.025em;
  color: var(--tone-fg, var(--txt-1));
  font-variant-numeric: tabular-nums;
}

/* 运行条目标记 */
.run-glyph {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: 8px;
  background: var(--surface-inset);
  color: var(--txt-2);
}
</style>
