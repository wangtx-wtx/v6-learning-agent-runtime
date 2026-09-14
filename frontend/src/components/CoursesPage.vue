<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { CoursesApi, ExamsApi } from '../api/endpoints'
import type { CalendarEvent, Course, CourseDeleteImpact, ExamEvent } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { statusLabel } from '../utils/format'
import { useToast } from '../composables/useToast'

const toast = useToast()

const courses = ref<Course[]>([])
const exams = ref<ExamEvent[]>([])
const loading = ref(true)
const error = ref('')

// 新增课程表单
const showCreate = ref(false)
const newCourse = ref<Partial<Course>>({ name: '', code: '', semester: '', teacher: '' })
const creating = ref(false)
const createError = ref('')

// 新增考试表单
const newExam = ref({ course_id: null as number | null, date: '', title: '期末考试', event_type: 'exam' as string, detail: '' })

const editingId = ref<number | null>(null)
const editingDate = ref('')

// 课程删除：影响预览 + 精确课程名二次确认。
const deleteTarget = ref<Course | null>(null)
const deleteImpact = ref<CourseDeleteImpact | null>(null)
const deleteConfirmName = ref('')
const deleteLoading = ref(false)
const deleteError = ref('')
const impactLabels: Record<string, string> = {
  chapters: '章节', lessons: '课时', materials: '材料关联', source_chunks: '材料索引',
  notes: '笔记', homeworks: '作业', errors: '错题关联', reviews: '复习记录',
  workflow_runs: '运行记录关联', graph_nodes: '知识图谱节点', academic_calendar: '考试/日历关联',
  course_schedule_rules: '固定课表规则',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cs, es] = await Promise.all([CoursesApi.list(), ExamsApi.list()])
    courses.value = cs
    exams.value = es
    if (courses.value.length && newExam.value.course_id === null) {
      newExam.value.course_id = courses.value[0].id
    }
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function addExam() {
  if (!newExam.value.course_id) {
    toast.error('请选择课程')
    return
  }
  if (!newExam.value.date.trim()) {
    toast.error('请选择日期')
    return
  }
  try {
    await ExamsApi.addCalendar({
      course_id: newExam.value.course_id,
      event_type: newExam.value.event_type,
      title: newExam.value.title,
      date: newExam.value.date,
      detail: newExam.value.detail,
    })
    newExam.value.date = ''
    newExam.value.detail = ''
    toast.success('已添加考试/校历事件')
    await load()
  } catch (e: any) {
    toast.error(`添加失败:${e?.message || e}`)
  }
}

function beginEdit(e: CalendarEvent) {
  editingId.value = e.id
  editingDate.value = e.date || ''
}

async function updateExam(e: CalendarEvent) {
  if (!editingDate.value) {
    toast.error('请选择新日期')
    return
  }
  try {
    await ExamsApi.updateCalendar(e.id, {
      course_id: e.course_id,
      event_type: e.event_type,
      title: e.title,
      date: editingDate.value,
      detail: e.detail || '',
    })
    editingId.value = null
    toast.success('已更新考试日期')
    await load()
  } catch (err: any) {
    toast.error(`更新失败:${err?.message || err}`)
  }
}

async function removeExam(id: number) {
  if (!confirm('确认删除这条考试安排吗？')) return
  try {
    await ExamsApi.deleteCalendar(id)
    toast.success('已删除考试安排')
    await load()
  } catch (err: any) {
    toast.error(`删除失败:${err?.message || err}`)
  }
}

async function createCourse() {
  if (!newCourse.value.name?.trim()) {
    toast.error('请填写课程名称')
    return
  }
  creating.value = true
  createError.value = ''
  try {
    await CoursesApi.create({
      name: newCourse.value.name,
      code: newCourse.value.code || undefined,
      semester: newCourse.value.semester || undefined,
      teacher: newCourse.value.teacher || undefined,
    })
    newCourse.value = { name: '', code: '', semester: '', teacher: '' }
    showCreate.value = false
    toast.success('课程已创建')
    await load()
  } catch (e: any) {
    const msg = e?.message || String(e)
    createError.value = msg
    toast.error(`创建失败:${msg}`)
  } finally {
    creating.value = false
  }
}

async function prepareDeleteCourse(course: Course) {
  deleteTarget.value = course
  deleteImpact.value = null
  deleteConfirmName.value = ''
  deleteError.value = ''
  deleteLoading.value = true
  try {
    deleteImpact.value = await CoursesApi.deleteImpact(course.id)
  } catch (e: any) {
    deleteError.value = e?.message || String(e)
  } finally {
    deleteLoading.value = false
  }
}

function closeDeleteCourse() {
  if (deleteLoading.value) return
  deleteTarget.value = null
  deleteImpact.value = null
  deleteConfirmName.value = ''
  deleteError.value = ''
}

async function confirmDeleteCourse() {
  const course = deleteTarget.value
  if (!course || deleteConfirmName.value !== course.name) return
  deleteLoading.value = true
  deleteError.value = ''
  try {
    await CoursesApi.delete(course.id, deleteConfirmName.value)
    if (newExam.value.course_id === course.id) newExam.value.course_id = null
    toast.success(`课程「${course.name}」已删除`)
    deleteLoading.value = false
    closeDeleteCourse()
    await load()
  } catch (e: any) {
    deleteError.value = e?.message || String(e)
    toast.error(`删除失败:${deleteError.value}`)
  } finally {
    deleteLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="book-open"
      title="课程与考试"
      subtitle="维护本学期课程和考试安排；固定课表与节假日请在「课表与校历」中管理。"
    >
      <template #actions>
        <button type="button" class="btn btn-primary" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '新增课程' }}
        </button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error">
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">课程列表</h3>
            <p class="card-muted">集中管理课程基本信息；删除前会预览关联影响并要求输入课程名称确认。</p>
          </div>
          <span class="chip">{{ courses.length }} 门</span>
        </div>
        <States :empty="!courses.length" empty-icon="inbox" empty-title="还没有课程">
          <template #empty>
            <button type="button" class="btn btn-secondary" @click="showCreate = true">新增第一门课程</button>
          </template>
          <div class="course-grid">
            <article v-for="c in courses" :key="c.id" class="card-soft course-card">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <p class="truncate text-sm font-semibold text-[var(--txt-1)]">{{ c.name }}</p>
                  <p class="course-meta">
                    <span>{{ c.code || '未填写编号' }}</span>
                    <span v-if="c.teacher">{{ c.teacher }}</span>
                  </p>
                </div>
                <span class="chip">{{ c.semester || '学期未指定' }}</span>
              </div>
              <div class="course-actions">
                <span class="course-hint">课程 #{{ c.id }}</span>
                <button type="button" class="course-delete" @click="prepareDeleteCourse(c)">删除课程</button>
              </div>
            </article>
          </div>
        </States>
      </section>

      <!-- 新增课程 -->
      <section v-if="showCreate" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">新增课程</h3>
            <p class="card-muted">填写课程名称、编号、学期和教师；课程名称为必填项。</p>
          </div>
        </div>
        <div class="grid gap-3 md:grid-cols-4">
          <div>
            <label class="label">课程名称</label>
            <input v-model="newCourse.name" class="input" placeholder="如：光学" />
          </div>
          <div>
            <label class="label">课程编号</label>
            <input v-model="newCourse.code" class="input" placeholder="如：PHYS2509" />
          </div>
          <div>
            <label class="label">学期</label>
            <input v-model="newCourse.semester" class="input" placeholder="如：2026 秋" />
          </div>
          <div>
            <label class="label">教师</label>
            <input v-model="newCourse.teacher" class="input" placeholder="可选" />
          </div>
        </div>
        <div class="mt-3 flex items-center gap-2">
          <button type="button" class="btn btn-primary" :disabled="creating" @click="createCourse">
            {{ creating ? '提交中...' : '保存课程' }}
          </button>
          <button type="button" class="btn btn-ghost" @click="showCreate = false">取消</button>
          <p v-if="createError" class="text-xs text-[var(--acc-red)]">{{ createError }}</p>
        </div>
      </section>

      <!-- 添加考试 -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">添加考试 / 校历事件</h3>
            <p class="card-muted">选择课程、日期与类型，事件会出现在 Dashboard 倒序提醒中。</p>
          </div>
        </div>
        <div class="grid gap-3 md:grid-cols-[1.5fr_1fr_1.5fr_auto]">
          <div>
            <label class="label">课程</label>
            <select v-model.number="newExam.course_id" class="input">
              <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
            </select>
          </div>
          <div>
            <label class="label">日期</label>
            <input v-model="newExam.date" type="date" class="input" />
          </div>
          <div>
            <label class="label">标题 / 备注</label>
            <input v-model="newExam.title" class="input" placeholder="期末考试" />
          </div>
          <div class="flex items-end">
            <button type="button" class="btn btn-primary w-full" @click="addExam">添加</button>
          </div>
        </div>
      </section>

      <!-- 考试安排表 -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">考试安排</h3>
            <p class="card-muted">支持改期、删除；按日期升序展示。</p>
          </div>
          <span class="chip">{{ exams.length }} 条</span>
        </div>
        <States :loading="loading" :error="error" :empty="!exams.length" empty-icon="calendar" empty-title="暂无考试安排">
          <div class="overflow-x-auto rounded-xl border border-slate-800">
            <table class="data-table bg-slate-950/40">
              <thead>
                <tr>
                  <th>课程</th>
                  <th>考试</th>
                  <th>日期</th>
                  <th>状态</th>
                  <th>备注</th>
                  <th class="text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in exams" :key="e.id">
                  <td class="font-medium">{{ e.course_name || `课程 #${e.course_id}` }}</td>
                  <td>{{ e.title }}</td>
                  <td>
                    <template v-if="editingId === e.id">
                      <input v-model="editingDate" type="date" class="input !w-36" />
                    </template>
                    <template v-else>
                      {{ e.date || '--' }}
                    </template>
                  </td>
                  <td>
                    <StatusBadge :status="e.event_type" variant="blue">{{ statusLabel(e.event_type) }}</StatusBadge>
                  </td>
                  <td class="text-xs text-slate-400">{{ e.detail || '—' }}</td>
                  <td class="text-right">
                    <template v-if="editingId === e.id">
                      <button type="button" class="btn btn-primary !px-2 !py-1 text-xs" @click="updateExam(e)">保存</button>
                      <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs" @click="editingId = null">取消</button>
                    </template>
                    <template v-else>
                      <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs" @click="beginEdit(e as CalendarEvent)">改期</button>
                      <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs text-[var(--acc-red)] hover:text-[var(--acc-red)]" @click="removeExam(e.id)">删除</button>
                    </template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </States>
      </section>
    </States>

    <Teleport to="body">
      <div v-if="deleteTarget" class="delete-backdrop" role="presentation">
        <section class="delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-course-title">
          <div>
            <span class="chip chip-red">高风险操作</span>
            <h3 id="delete-course-title" class="delete-title">删除课程「{{ deleteTarget.name }}」</h3>
            <p class="delete-copy">课程及其关联学习数据将被永久删除或解除关联，此操作无法撤销。</p>
          </div>

          <div class="impact-panel">
            <p class="impact-heading">关联影响预览</p>
            <p v-if="deleteLoading && !deleteImpact" class="delete-copy">正在检查关联数据…</p>
            <p v-else-if="deleteError" class="delete-error">{{ deleteError }}</p>
            <div v-else-if="deleteImpact" class="impact-grid">
              <div v-for="(count, key) in deleteImpact.counts" :key="key" class="impact-item">
                <span>{{ impactLabels[String(key)] || key }}</span>
                <strong>{{ count }}</strong>
              </div>
            </div>
          </div>

          <div>
            <label class="label" for="course-delete-confirm">
              输入完整课程名称 <strong class="text-[var(--txt-1)]">{{ deleteTarget.name }}</strong> 以确认
            </label>
            <input
              id="course-delete-confirm"
              v-model="deleteConfirmName"
              class="input"
              autocomplete="off"
              :disabled="deleteLoading"
              placeholder="请输入完整课程名称"
              @keyup.enter="confirmDeleteCourse"
            />
          </div>

          <div class="delete-footer">
            <button type="button" class="btn btn-ghost" :disabled="deleteLoading" @click="closeDeleteCourse">取消</button>
            <button
              type="button"
              class="btn btn-danger-solid"
              :disabled="deleteLoading || !deleteImpact || deleteConfirmName !== deleteTarget.name"
              @click="confirmDeleteCourse"
            >
              {{ deleteLoading ? '处理中…' : '永久删除课程' }}
            </button>
          </div>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.course-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 12px;
}

.course-card {
  display: flex;
  min-height: 112px;
  flex-direction: column;
  justify-content: space-between;
  gap: 18px;
}

.course-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--txt-3);
}

.course-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-top: 1px solid var(--line);
  padding-top: 10px;
}

.course-hint { font-size: 11px; color: var(--txt-3); }
.course-delete {
  border-radius: 7px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--acc-red);
  transition: background 0.15s ease;
}
.course-delete:hover { background: var(--acc-red-soft); }

.delete-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.52);
  backdrop-filter: blur(8px);
}

.delete-dialog {
  width: min(560px, 100%);
  max-height: calc(100vh - 40px);
  overflow: auto;
  border: 1px solid var(--line-strong);
  border-radius: 18px;
  padding: 22px;
  background: var(--surface-1);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.36);
}

.delete-title { margin-top: 12px; font-size: 19px; font-weight: 650; color: var(--txt-1); }
.delete-copy { margin-top: 6px; font-size: 13px; line-height: 1.6; color: var(--txt-2); }
.delete-error { margin-top: 6px; font-size: 13px; color: var(--acc-red); }
.impact-panel {
  margin: 18px 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 13px;
  background: var(--surface-2);
}
.impact-heading { margin-bottom: 9px; font-size: 12px; font-weight: 600; color: var(--txt-2); }
.impact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; }
.impact-item { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--txt-2); }
.impact-item strong { color: var(--txt-1); font-variant-numeric: tabular-nums; }
.delete-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.btn-danger-solid { border-color: var(--acc-red); background: var(--acc-red); color: #fff; }
.btn-danger-solid:not(:disabled):hover { filter: brightness(0.94); }

@media (max-width: 520px) {
  .delete-dialog { padding: 17px; }
  .impact-grid { grid-template-columns: 1fr; }
  .delete-footer { flex-direction: column-reverse; }
  .delete-footer .btn { width: 100%; }
}
</style>
