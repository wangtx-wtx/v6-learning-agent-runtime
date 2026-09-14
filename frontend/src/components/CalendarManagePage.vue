<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  CoursesApi,
  ScheduleApi,
  type AcademicTerm,
  type AdjustmentType,
  type CalendarAdjustment,
  type Course,
  type EffectiveSchedule,
  type ScheduleRule,
} from '../api/endpoints'
import { useToast } from '../composables/useToast'
import Icon from './widgets/Icon.vue'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'

const toast = useToast()
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const courses = ref<Course[]>([])
const terms = ref<AcademicTerm[]>([])
const rules = ref<ScheduleRule[]>([])
const adjustments = ref<CalendarAdjustment[]>([])
const preview = ref<EffectiveSchedule | null>(null)

const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const parityLabels = { all: '每周', odd: '单周', even: '双周' }
const adjustmentLabels: Record<AdjustmentType, string> = {
  day_off: '调休',
  holiday: '节假日',
  workday: '补班',
}

function localIso(value = new Date()): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 10)
}

function addDays(value: string, count: number): string {
  const day = new Date(`${value}T12:00:00`)
  day.setDate(day.getDate() + count)
  return localIso(day)
}

const termForm = ref<Partial<AcademicTerm>>({
  name: '当前学期', school_year: '', semester: '', start_date: localIso(),
  end_date: addDays(localIso(), 112), first_week_monday: localIso(),
  teaching_weeks: 16, exam_start: '', exam_end: '', source: 'manual', active: true,
})

const ruleForm = ref<Partial<ScheduleRule>>({
  course_id: undefined, weekday: 1, start_week: 1, end_week: 16,
  week_parity: 'all', periods: [], start_time: '', end_time: '',
  location: '', teacher: '', note: '', source: 'manual', enabled: true,
})
const rulePeriods = ref('')

const adjustmentForm = ref<Partial<CalendarAdjustment>>({
  adjustment_type: 'day_off', date: localIso(), source_date: '', periods: [],
  title: '', detail: '', source: 'manual',
})
const adjustmentPeriods = ref('')

const activeTerm = computed(() => terms.value.find(term => Boolean(term.active)) || terms.value[0])
const selectedAdjustmentType = computed(() => adjustmentForm.value.adjustment_type || 'day_off')

function parsePeriods(value: string): number[] {
  return [...new Set(value.split(/[,，\s-]+/).map(Number).filter(n => Number.isInteger(n) && n > 0 && n <= 20))]
    .sort((a, b) => a - b)
}

function periodText(periods?: number[]): string {
  if (!periods?.length) return '节次不限'
  return periods.length === 1 ? `第 ${periods[0]} 节` : `第 ${periods[0]}–${periods[periods.length - 1]} 节`
}

function resetRule() {
  ruleForm.value = {
    course_id: undefined, weekday: 1, start_week: 1,
    end_week: activeTerm.value?.teaching_weeks || 16, week_parity: 'all', periods: [],
    start_time: '', end_time: '', location: '', teacher: '', note: '', source: 'manual', enabled: true,
  }
  rulePeriods.value = ''
}

function resetAdjustment() {
  adjustmentForm.value = {
    adjustment_type: 'day_off', date: localIso(), source_date: '', periods: [],
    title: '', detail: '', source: 'manual',
  }
  adjustmentPeriods.value = ''
}

function editTerm(term: AcademicTerm) {
  termForm.value = { ...term, active: Boolean(term.active) }
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function editRule(rule: ScheduleRule) {
  ruleForm.value = { ...rule, enabled: Boolean(rule.enabled) }
  rulePeriods.value = rule.periods.join(',')
}

function editAdjustment(item: CalendarAdjustment) {
  adjustmentForm.value = { ...item }
  adjustmentPeriods.value = item.periods.join(',')
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [courseRows, termRows, ruleRows, adjustmentRows, effective] = await Promise.all([
      CoursesApi.list(), ScheduleApi.terms(), ScheduleApi.rules(),
      ScheduleApi.adjustments(), ScheduleApi.effective({ days: 7 }),
    ])
    courses.value = courseRows
    terms.value = termRows
    rules.value = ruleRows
    adjustments.value = adjustmentRows
    preview.value = effective
    if (activeTerm.value && !termForm.value.id) editTerm(activeTerm.value)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function saveTerm() {
  saving.value = true
  try {
    if (!termForm.value.name || !termForm.value.start_date || !termForm.value.end_date || !termForm.value.first_week_monday) {
      throw new Error('请填写学期名称、起止日期和第一教学周周一')
    }
    if (termForm.value.id) await ScheduleApi.updateTerm(termForm.value.id, termForm.value)
    else await ScheduleApi.createTerm(termForm.value)
    toast.success('学期设置已保存')
    await loadAll()
  } catch (e: any) {
    toast.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

async function saveRule() {
  saving.value = true
  try {
    if (!ruleForm.value.course_id) throw new Error('请选择课程')
    const payload = { ...ruleForm.value, periods: parsePeriods(rulePeriods.value) }
    if (ruleForm.value.id) await ScheduleApi.updateRule(ruleForm.value.id, payload)
    else await ScheduleApi.createRule(payload)
    toast.success(ruleForm.value.id ? '固定课表已更新' : '固定课表已添加')
    resetRule()
    await loadAll()
  } catch (e: any) {
    toast.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

async function removeRule(rule: ScheduleRule) {
  if (!confirm(`确认删除「${rule.course_name || '该课程'}」的固定课表吗？`)) return
  await ScheduleApi.deleteRule(rule.id)
  toast.success('固定课表已删除')
  await loadAll()
}

async function saveAdjustment() {
  saving.value = true
  try {
    if (!adjustmentForm.value.date) throw new Error('请选择生效日期')
    if (selectedAdjustmentType.value === 'workday' && !adjustmentForm.value.source_date) {
      throw new Error('请选择补班要补哪一天的课程')
    }
    const payload = {
      ...adjustmentForm.value,
      periods: parsePeriods(adjustmentPeriods.value),
    }
    if (adjustmentForm.value.id) await ScheduleApi.updateAdjustment(adjustmentForm.value.id, payload)
    else await ScheduleApi.createAdjustment(payload)
    toast.success(adjustmentForm.value.id ? '临时调整已更新' : '临时调整已添加')
    resetAdjustment()
    await loadAll()
  } catch (e: any) {
    toast.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

async function removeAdjustment(item: CalendarAdjustment) {
  if (!confirm(`确认删除这条“${adjustmentLabels[item.adjustment_type]}”安排吗？`)) return
  await ScheduleApi.deleteAdjustment(item.id)
  toast.success('临时调整已删除')
  await loadAll()
}

onMounted(loadAll)
</script>

<template>
  <div class="page-shell">
    <PageHeader icon="calendar" title="课表与校历" subtitle="维护学期基准、固定课表和调课调休；总览只展示这里计算出的最终结果。">
      <template #actions>
        <button type="button" class="btn btn-secondary" :disabled="loading" @click="loadAll">
          <Icon name="refresh" :size="15" /> 刷新
        </button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error">
      <div class="space-y-4">
        <section class="section">
          <div class="section-head"><div><h3 class="card-title">临时调整</h3><p class="card-muted">仅保留调休、节假日和补班；补班按指定来源日期的实际教学周课表执行。</p></div><span class="chip">{{ adjustments.length }} 条</span></div>
          <div class="form-grid adjustment-grid">
            <label><span class="label">类型</span><select v-model="adjustmentForm.adjustment_type" class="input"><option v-for="(label, key) in adjustmentLabels" :key="key" :value="key">{{ label }}</option></select></label>
            <label><span class="label">生效日期</span><input v-model="adjustmentForm.date" class="input" type="date" /></label>
            <label v-if="selectedAdjustmentType === 'workday'"><span class="label">补哪一天的课程</span><input v-model="adjustmentForm.source_date" class="input" type="date" /></label>
            <label v-if="selectedAdjustmentType === 'day_off'"><span class="label">影响节次（留空为全天）</span><input v-model="adjustmentPeriods" class="input" placeholder="如 1-10；留空表示全天" /></label>
            <label><span class="label">标题</span><input v-model="adjustmentForm.title" class="input" :placeholder="adjustmentLabels[selectedAdjustmentType]" /></label>
            <label class="md:col-span-2"><span class="label">备注</span><input v-model="adjustmentForm.detail" class="input" placeholder="通知来源或其他说明" /></label>
          </div>
          <div class="mt-3 flex justify-end gap-2"><button v-if="adjustmentForm.id" type="button" class="btn btn-secondary" @click="resetAdjustment">取消编辑</button><button type="button" class="btn btn-primary" :disabled="saving" @click="saveAdjustment">{{ adjustmentForm.id ? '保存修改' : '添加调整' }}</button></div>
          <div class="mt-4 space-y-2">
            <div v-for="item in adjustments" :key="item.id" class="card-soft flex flex-wrap items-center justify-between gap-3">
              <div><div class="flex items-center gap-2"><span class="chip chip-blue">{{ adjustmentLabels[item.adjustment_type] }}</span><strong class="text-[13px] text-t1">{{ item.title || '日程调整' }}</strong></div><p class="mt-1.5 text-[11.5px] text-t3">{{ item.date }}<template v-if="item.source_date"> · 补 {{ item.source_date }} 的课程</template><template v-if="item.periods.length"> · 第 {{ item.periods.join(',') }} 节</template><template v-if="item.detail"> · {{ item.detail }}</template></p></div>
              <div class="flex gap-2"><button type="button" class="btn btn-secondary" @click="editAdjustment(item)">编辑</button><button type="button" class="btn btn-danger" @click="removeAdjustment(item)">删除</button></div>
            </div>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div><h3 class="card-title">学期设置</h3><p class="card-muted">第一教学周周一用于将自然日期换算成教学周。</p></div>
            <span v-if="activeTerm" class="chip chip-blue">当前：{{ activeTerm.name }}</span>
          </div>
          <div class="form-grid term-grid">
            <label><span class="label">学期名称</span><input v-model="termForm.name" class="input" placeholder="2026–2027 第一学期" /></label>
            <label><span class="label">学年</span><input v-model="termForm.school_year" class="input" placeholder="2026-2027" /></label>
            <label><span class="label">学期代码</span><input v-model="termForm.semester" class="input" placeholder="2026-2027-1" /></label>
            <label><span class="label">教学周数</span><input v-model.number="termForm.teaching_weeks" class="input" type="number" min="1" max="30" /></label>
            <label><span class="label">学期开始</span><input v-model="termForm.start_date" class="input" type="date" /></label>
            <label><span class="label">第一周周一</span><input v-model="termForm.first_week_monday" class="input" type="date" /></label>
            <label><span class="label">学期结束</span><input v-model="termForm.end_date" class="input" type="date" /></label>
            <label><span class="label">考试周开始</span><input v-model="termForm.exam_start" class="input" type="date" /></label>
            <label><span class="label">考试周结束</span><input v-model="termForm.exam_end" class="input" type="date" /></label>
          </div>
          <div class="mt-3 flex justify-end"><button type="button" class="btn btn-primary" :disabled="saving" @click="saveTerm">保存学期设置</button></div>
        </section>

        <section class="section">
          <div class="section-head">
            <div><h3 class="card-title">固定课程表</h3><p class="card-muted">支持每周、单周、双周以及不同起止教学周。</p></div>
            <span class="chip">{{ rules.length }} 条</span>
          </div>
          <div class="form-grid rule-grid">
            <label><span class="label">课程</span><select v-model.number="ruleForm.course_id" class="input"><option :value="undefined">请选择</option><option v-for="course in courses" :key="course.id" :value="course.id">{{ course.name }}</option></select></label>
            <label><span class="label">星期</span><select v-model.number="ruleForm.weekday" class="input"><option v-for="(name, i) in weekdays" :key="name" :value="i + 1">{{ name }}</option></select></label>
            <label><span class="label">开始周</span><input v-model.number="ruleForm.start_week" class="input" type="number" min="1" /></label>
            <label><span class="label">结束周</span><input v-model.number="ruleForm.end_week" class="input" type="number" min="1" /></label>
            <label><span class="label">单双周</span><select v-model="ruleForm.week_parity" class="input"><option value="all">每周</option><option value="odd">单周</option><option value="even">双周</option></select></label>
            <label><span class="label">节次</span><input v-model="rulePeriods" class="input" placeholder="如 1,2 或 8,9" /></label>
            <label><span class="label">开始时间</span><input v-model="ruleForm.start_time" class="input" type="time" /></label>
            <label><span class="label">结束时间</span><input v-model="ruleForm.end_time" class="input" type="time" /></label>
            <label><span class="label">教室</span><input v-model="ruleForm.location" class="input" placeholder="闵二教202" /></label>
            <label><span class="label">教师</span><input v-model="ruleForm.teacher" class="input" /></label>
          </div>
          <div class="mt-3 flex justify-end gap-2"><button v-if="ruleForm.id" type="button" class="btn btn-secondary" @click="resetRule">取消编辑</button><button type="button" class="btn btn-primary" :disabled="saving" @click="saveRule">{{ ruleForm.id ? '保存修改' : '添加固定课程' }}</button></div>
          <div class="mt-4 overflow-x-auto">
            <table class="data-table"><thead><tr><th>课程</th><th>时间</th><th>周次</th><th>地点 / 教师</th><th></th></tr></thead>
              <tbody><tr v-for="rule in rules" :key="rule.id"><td>{{ rule.course_name }}</td><td>{{ weekdays[rule.weekday - 1] }} · {{ periodText(rule.periods) }}</td><td>第 {{ rule.start_week }}–{{ rule.end_week }} 周 · {{ parityLabels[rule.week_parity] }}</td><td>{{ rule.location || '地点待定' }}<span v-if="rule.teacher" class="block text-t3">{{ rule.teacher }}</span></td><td><div class="flex justify-end gap-2"><button type="button" class="btn btn-secondary" @click="editRule(rule)">编辑</button><button type="button" class="btn btn-danger" @click="removeRule(rule)">删除</button></div></td></tr></tbody>
            </table>
          </div>
        </section>

        <section class="section">
          <div class="section-head"><div><h3 class="card-title">未来七天结果预览</h3><p class="card-muted">这里与总览使用同一个后端计算结果。</p></div><RouterLink class="btn btn-secondary" to="/">返回总览</RouterLink></div>
          <div class="preview-grid">
            <article v-for="day in preview?.days || []" :key="day.date" class="preview-day" :class="{ today: day.is_today, off: day.is_day_off }">
              <div class="flex items-start justify-between gap-2"><div><strong>{{ weekdays[day.weekday - 1] }}</strong><p class="text-[11px] text-t3">{{ day.date }}</p></div><span v-if="day.week_number" class="chip">第{{ day.week_number }}周</span></div>
              <p v-for="event in day.events" :key="event.id" class="mt-2 text-[11px] font-semibold text-[var(--acc-orange)]">{{ event.title || adjustmentLabels[event.adjustment_type] }}</p>
              <div v-for="course in day.courses" :key="course.id" class="preview-course"><strong>{{ course.course_name }}</strong><span>{{ periodText(course.periods) }} · {{ course.location || '地点待定' }}</span></div>
              <p v-if="!day.courses.length" class="mt-6 text-center text-[11px] text-t3">{{ day.is_day_off ? '放假 / 调休' : '无课' }}</p>
            </article>
          </div>
        </section>
      </div>
    </States>
  </div>
</template>

<style scoped>
.form-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.term-grid, .rule-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.adjustment-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.preview-grid { display: grid; grid-template-columns: repeat(7, minmax(145px, 1fr)); gap: 9px; overflow-x: auto; }
.preview-day { min-height: 170px; padding: 10px; border: 1px solid var(--line); border-radius: var(--r-ctl); background: var(--surface-inset); }
.preview-day.today { border-color: var(--accent); box-shadow: inset 0 2px 0 var(--accent); }
.preview-day.off { background: var(--acc-orange-soft); }
.preview-course { display: flex; flex-direction: column; gap: 3px; margin-top: 8px; padding: 7px; border-left: 3px solid var(--accent); border-radius: 7px; background: var(--surface-1); font-size: 11px; }
.preview-course span { color: var(--txt-3); }
.btn-danger { color: var(--acc-red); border-color: var(--acc-red-line); }
.btn-danger:hover { background: var(--acc-red-soft); }
@media (max-width: 1100px) { .term-grid, .rule-grid, .adjustment-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px) { .form-grid, .term-grid, .rule-grid, .adjustment-grid { grid-template-columns: 1fr; } }
</style>
