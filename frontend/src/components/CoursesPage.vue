<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { CoursesApi, ExamsApi, SyllabusApi } from '../api/endpoints'
import type { CalendarEvent, Course, ExamEvent } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { fmtDate, safeJson, statusLabel } from '../utils/format'
import { useToast } from '../composables/useToast'

interface Schedule {
  weekday?: string
  start?: string
  end?: string
  location?: string
}

const toast = useToast()

const courses = ref<Course[]>([])
const exams = ref<ExamEvent[]>([])
const loading = ref(true)
const error = ref('')
const seedLoading = ref(false)
const seedMessage = ref('')

// 新增课程表单
const showCreate = ref(false)
const newCourse = ref<Partial<Course>>({ name: '', code: '', semester: '', teacher: '' })
const creating = ref(false)
const createError = ref('')

// 新增考试表单
const newExam = ref({ course_id: null as number | null, date: '', title: '期末考试', event_type: 'exam' as string, detail: '' })

// 教学大纲导入
const showSyllabus = ref(false)
const syllabusJson = ref('')
const syllabusLoading = ref(false)
const syllabusMessage = ref('')
const syllabusError = ref('')

const editingId = ref<number | null>(null)
const editingDate = ref('')

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

async function seedCalendar(force = false) {
  seedLoading.value = true
  seedMessage.value = ''
  try {
    const r: any = await ExamsApi.seed(force)
    const ok = `校历同步完成:${r.count ?? r.added ?? '?'} 条已收录`
    seedMessage.value = ok
    toast.success(ok)
    await load()
  } catch (e: any) {
    const msg = e?.message || String(e)
    seedMessage.value = `校历同步失败:${msg}`
    toast.error(`校历同步失败:${msg}`)
  } finally {
    seedLoading.value = false
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

function parseSchedule(s?: string | null): Schedule[] {
  const obj = safeJson<any>(s, {})
  if (!obj) return []
  if (Array.isArray(obj)) return obj as Schedule[]
  if (Array.isArray(obj.lessons)) return obj.lessons as Schedule[]
  return []
}

async function importSyllabus() {
  if (!syllabusJson.value.trim()) {
    toast.error('请粘贴 JSON 内容')
    return
  }
  syllabusLoading.value = true
  syllabusMessage.value = ''
  syllabusError.value = ''
  let body: any
  try {
    body = JSON.parse(syllabusJson.value)
  } catch (e: any) {
    syllabusError.value = `JSON 解析失败:${e?.message || e}`
    syllabusLoading.value = false
    toast.error(`JSON 解析失败:${e?.message || e}`)
    return
  }
  try {
    const r = await SyllabusApi.import(body)
    syllabusMessage.value = `导入成功:课程 #${r.course_id},新增章节 ${r.stats.chapters} 个、课时 ${r.stats.lessons} 个。`
    syllabusJson.value = ''
    showSyllabus.value = false
    toast.success('教学大纲已导入')
    await load()
  } catch (e: any) {
    syllabusError.value = `导入失败:${e?.message || e}`
    toast.error(`导入失败:${e?.message || e}`)
  } finally {
    syllabusLoading.value = false
  }
}

const syllabusExample = JSON.stringify({
  course_code: 'PHYS2509',
  course_name: '光学',
  chapters: [
    { chapter_no: 1, title: '几何光学', lessons: ['L01 反射与折射', 'L02 透镜成像'] },
    { chapter_no: 2, title: '波动光学', lessons: ['L03 干涉', { lesson_no: 'L04', title: '衍射' }] },
  ],
}, null, 2)

onMounted(load)
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="📚"
      title="课程与考试"
      subtitle="维护本学期课程信息、校历、考试安排；导入教学大纲自动生成章节和课时。"
    >
      <template #actions>
        <button class="btn btn-secondary" :disabled="seedLoading" @click="seedCalendar(false)">
          {{ seedLoading ? '同步中...' : '同步校历' }}
        </button>
        <button class="btn btn-secondary" @click="showSyllabus = !showSyllabus">
          {{ showSyllabus ? '收起大纲导入' : '导入教学大纲' }}
        </button>
        <button class="btn btn-primary" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '新增课程' }}
        </button>
      </template>
    </PageHeader>

    <p v-if="seedMessage" class="text-xs text-slate-400">{{ seedMessage }}</p>

    <States :loading="loading" :error="error">
      <!-- 课程卡片网格 -->
      <section class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">课程列表</h3>
            <p class="card-muted">展示本学期所有课程，附带上课时间、教师和学期。</p>
          </div>
          <span class="chip">{{ courses.length }} 门</span>
        </div>
        <States :empty="!courses.length" empty-icon="📭" empty-title="还没有课程">
          <template #empty>
            <button class="btn btn-secondary" @click="showCreate = true">新增第一门课程</button>
          </template>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <article
              v-for="c in courses"
              :key="c.id"
              class="card-soft"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <p class="truncate text-sm font-semibold text-slate-100">{{ c.name }}</p>
                  <p class="text-xs text-slate-500">
                    <span v-if="c.code">{{ c.code }}</span>
                    <span v-if="c.teacher"> · {{ c.teacher }}</span>
                  </p>
                </div>
                <span class="chip">{{ c.semester || '学期未指定' }}</span>
              </div>
              <div class="mt-3 space-y-1 text-[11px] text-slate-400">
                <p>创建：{{ fmtDate(c.created_at) }}</p>
                <div v-if="parseSchedule(c.schedule_json).length" class="space-y-0.5">
                  <p
                    v-for="(s, i) in parseSchedule(c.schedule_json)"
                    :key="i"
                    class="rounded-md bg-slate-950/60 px-2 py-1"
                  >
                    {{ s.weekday || '?' }} {{ s.start || '' }}{{ s.end ? '–' + s.end : '' }}
                    <span v-if="s.location" class="text-slate-500">· {{ s.location }}</span>
                  </p>
                </div>
                <p v-else class="text-slate-600">未排课</p>
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
            <p class="card-muted">填写课程基本信息；教学大纲可通过下方「导入教学大纲」批量补全。</p>
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
          <button class="btn btn-primary" :disabled="creating" @click="createCourse">
            {{ creating ? '提交中...' : '保存课程' }}
          </button>
          <button class="btn btn-ghost" @click="showCreate = false">取消</button>
          <p v-if="createError" class="text-xs text-rose-300">{{ createError }}</p>
        </div>
      </section>

      <!-- 教学大纲导入 -->
      <section v-if="showSyllabus" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">导入教学大纲 JSON</h3>
            <p class="card-muted">支持粘贴 ECNU 等来源的章节课时 JSON，缺失课程会自动新建。</p>
          </div>
        </div>
        <div class="grid gap-3 lg:grid-cols-2">
          <div>
            <label class="label">JSON 内容</label>
            <textarea v-model="syllabusJson" rows="14" class="input font-mono text-xs" :placeholder="syllabusExample" />
            <p v-if="syllabusError" class="mt-2 text-xs text-rose-300">{{ syllabusError }}</p>
            <p v-if="syllabusMessage" class="mt-2 text-xs text-emerald-300">{{ syllabusMessage }}</p>
            <div class="mt-3 flex gap-2">
              <button class="btn btn-primary" :disabled="syllabusLoading" @click="importSyllabus">
                {{ syllabusLoading ? '导入中...' : '开始导入' }}
              </button>
              <button class="btn btn-ghost" @click="syllabusJson = syllabusExample">填入示例</button>
            </div>
          </div>
          <div>
            <label class="label">格式说明</label>
            <pre class="code-panel">{{ syllabusExample }}</pre>
            <ul class="mt-3 space-y-1 text-xs text-slate-400">
              <li>· <code class="text-slate-200">course_code</code> / <code class="text-slate-200">course_name</code> 至少填一项，已存在则复用。</li>
              <li>· <code class="text-slate-200">chapters[].chapter_no</code> 为可选整数；缺省时按顺序生成。</li>
              <li>· <code class="text-slate-200">lessons</code> 可填字符串或 <code class="text-slate-200">{ lesson_no, title, date }</code>。</li>
              <li>· 重复章节（按课程 + 标题）会被跳过，仅追加缺失课时。</li>
            </ul>
          </div>
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
            <button class="btn btn-primary w-full" @click="addExam">添加</button>
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
          <div class="flex gap-2">
            <span class="chip">{{ exams.length }} 条</span>
            <button class="btn btn-ghost text-xs" @click="seedCalendar(true)">强制重新同步</button>
          </div>
        </div>
        <States :loading="loading" :error="error" :empty="!exams.length" empty-icon="📅" empty-title="暂无考试安排">
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
                      <button class="btn btn-primary !px-2 !py-1 text-xs" @click="updateExam(e)">保存</button>
                      <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="editingId = null">取消</button>
                    </template>
                    <template v-else>
                      <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="beginEdit(e as CalendarEvent)">改期</button>
                      <button class="btn btn-ghost !px-2 !py-1 text-xs text-rose-300 hover:text-rose-200" @click="removeExam(e.id)">删除</button>
                    </template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </States>
      </section>
    </States>
  </div>
</template>