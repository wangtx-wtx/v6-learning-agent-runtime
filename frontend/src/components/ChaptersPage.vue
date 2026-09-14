<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ChaptersApi, CoursesApi } from '../api/endpoints'
import type { Chapter, Course } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { chapterStatusLabel, chapterStatusTone } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([]), chapters = ref<Chapter[]>([])
const loading = ref(false), error = ref(''), filterCourseId = ref('all')
const selectedCourse = ref<Course | null>(null), savingId = ref<number | 'new' | null>(null)
const deleteTarget = ref<Chapter | null>(null), deleteConfirmTitle = ref('')
const drafts = reactive<Record<number, { chapter_no: number | null; title: string; notes: string; status: string }>>({})
const newChapter = reactive({ chapter_no: null as number | null, title: '', notes: '' })
const toast = useToast()

async function load() {
  loading.value = true; error.value = ''
  try { [courses.value, chapters.value] = await Promise.all([CoursesApi.list(), ChaptersApi.list()]) }
  catch (e: any) { error.value = e?.message || String(e) }
  finally { loading.value = false }
}
function chaptersFor(id: number) { return chapters.value.filter(x => x.course_id === id).sort((a, b) => (a.chapter_no ?? 9999) - (b.chapter_no ?? 9999)) }
const visibleCourses = computed(() => filterCourseId.value === 'all' ? courses.value : courses.value.filter(c => c.id === Number(filterCourseId.value)))
const statusGroups = computed(() => {
  const out: Record<string, number> = { not_started: 0, in_progress: 0, completed: 0, reviewed: 0 }
  chapters.value.forEach(ch => { out[ch.status || 'not_started'] = (out[ch.status || 'not_started'] || 0) + 1 }); return out
})
function openCourse(course: Course) {
  selectedCourse.value = course
  chaptersFor(course.id).forEach(ch => { drafts[ch.id] = { chapter_no: ch.chapter_no, title: ch.title, notes: ch.notes || '', status: ch.status || 'not_started' } })
  Object.assign(newChapter, { chapter_no: null, title: '', notes: '' })
}
function closeCourse() { selectedCourse.value = null; deleteTarget.value = null; deleteConfirmTitle.value = '' }
async function createChapter() {
  if (!selectedCourse.value || !newChapter.title.trim()) return toast.error('请填写章节名称')
  savingId.value = 'new'
  try {
    await ChaptersApi.create({ course_id: selectedCourse.value.id, chapter_no: newChapter.chapter_no, title: newChapter.title.trim(), notes: newChapter.notes.trim() })
    await load(); Object.assign(newChapter, { chapter_no: null, title: '', notes: '' }); openCourse(selectedCourse.value); toast.success('章节已新增')
  } catch (e: any) { toast.error(`新增失败：${e?.message || e}`) } finally { savingId.value = null }
}
async function saveChapter(ch: Chapter) {
  const draft = drafts[ch.id]; if (!draft?.title.trim()) return toast.error('章节名称不能为空')
  savingId.value = ch.id
  try { await ChaptersApi.update(ch.id, { ...draft, title: draft.title.trim(), notes: draft.notes.trim() }); await load(); openCourse(selectedCourse.value!); toast.success('章节与备注已保存') }
  catch (e: any) { toast.error(`保存失败：${e?.message || e}`) } finally { savingId.value = null }
}
function requestDelete(ch: Chapter) { deleteTarget.value = ch; deleteConfirmTitle.value = '' }
async function confirmDelete() {
  if (!deleteTarget.value || deleteConfirmTitle.value !== deleteTarget.value.title) return
  savingId.value = deleteTarget.value.id
  try { await ChaptersApi.delete(deleteTarget.value.id, deleteConfirmTitle.value); deleteTarget.value = null; deleteConfirmTitle.value = ''; await load(); openCourse(selectedCourse.value!); toast.success('章节已删除') }
  catch (e: any) { toast.error(`删除失败：${e?.message || e}`) } finally { savingId.value = null }
}
onMounted(load)
</script>

<template>
  <div class="page-shell">
    <PageHeader icon="layers" title="章节进度" subtitle="按课程手动维护章节、学习状态与备注。点击课程卡片进入管理。">
      <template #actions>
        <select v-model="filterCourseId" class="input !w-56"><option value="all">全部课程（{{ courses.length }}）</option><option v-for="c in courses" :key="c.id" :value="String(c.id)">{{ c.name }}</option></select>
        <button type="button" class="btn btn-secondary" @click="load">刷新</button>
      </template>
    </PageHeader>

    <section class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div v-for="(count, key) in statusGroups" :key="key" class="card-soft !p-4"><p class="text-xs text-slate-400">{{ chapterStatusLabel(String(key)) }}</p><p class="mt-1 text-2xl font-bold text-slate-100">{{ count }}</p></div>
    </section>

    <States :loading="loading" :error="error" :empty="!courses.length" empty-icon="layers" empty-title="还没有课程">
      <section class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <button v-for="course in visibleCourses" :key="course.id" type="button" class="card-soft group !p-5 text-left transition hover:-translate-y-0.5 hover:border-sky-400/50 hover:bg-sky-500/5" @click="openCourse(course)">
          <div class="flex items-start justify-between gap-3"><div class="min-w-0"><h3 class="truncate text-base font-semibold text-slate-100">{{ course.name }}</h3><p class="mt-1 text-xs text-slate-500">{{ course.code || '未设置课程代码' }} · {{ course.teacher || '未指定教师' }}</p></div><span class="rounded-full bg-sky-500/10 px-2.5 py-1 text-xs font-medium text-sky-300">{{ chaptersFor(course.id).length }} 章</span></div>
          <div class="mt-5 flex items-center justify-between border-t border-white/10 pt-4"><span class="text-xs text-slate-400">查看并编辑章节、状态和备注</span><span class="text-sky-400 transition group-hover:translate-x-1">→</span></div>
        </button>
      </section>
    </States>

    <Teleport to="body">
    <div v-if="selectedCourse" class="chapter-backdrop" @click.self="closeCourse">
      <section role="dialog" aria-modal="true" class="chapter-dialog">
        <header class="chapter-dialog-header"><div><p class="text-xs font-semibold uppercase tracking-widest text-sky-400">章节管理</p><h2 class="mt-1 text-2xl font-bold text-slate-50">{{ selectedCourse.name }}</h2><p class="mt-1 text-sm text-slate-400">填写实际章节名称、当前进度与课堂备注。</p></div><button type="button" class="btn btn-secondary" @click="closeCourse">关闭</button></header>

        <div class="space-y-4">
          <article v-for="ch in chaptersFor(selectedCourse.id)" :key="ch.id" class="rounded-xl border border-white/10 bg-slate-950/35 p-4">
            <div class="grid gap-3 md:grid-cols-[110px_1fr_180px]">
              <label class="text-xs text-slate-400">章节序号<input v-model.number="drafts[ch.id].chapter_no" type="number" min="1" class="input mt-1 w-full" placeholder="例如 1"></label>
              <label class="text-xs text-slate-400">章节名称<input v-model="drafts[ch.id].title" class="input mt-1 w-full" placeholder="例如：第一章 绪论"></label>
              <label class="text-xs text-slate-400">学习状态<select v-model="drafts[ch.id].status" class="input mt-1 w-full"><option value="not_started">未开始</option><option value="in_progress">学习中</option><option value="completed">已完成</option><option value="reviewed">已复习</option></select></label>
            </div>
            <label class="mt-3 block text-xs text-slate-400">章节备注<textarea v-model="drafts[ch.id].notes" rows="3" class="input mt-1 !h-auto w-full resize-y" placeholder="记录授课范围、教师强调内容、未掌握知识点或后续计划"></textarea></label>
            <div class="mt-3 flex justify-between gap-2"><StatusBadge :status="ch.status" :variant="chapterStatusTone(ch.status)">{{ chapterStatusLabel(ch.status) }}</StatusBadge><div class="flex gap-2"><button type="button" class="btn btn-secondary !px-3 !py-1.5 text-xs text-rose-300" @click="requestDelete(ch)">删除</button><button type="button" class="btn btn-primary !px-4 !py-1.5 text-xs" :disabled="savingId === ch.id" @click="saveChapter(ch)">{{ savingId === ch.id ? '保存中…' : '保存修改' }}</button></div></div>
          </article>
          <div v-if="!chaptersFor(selectedCourse.id).length" class="rounded-xl border border-dashed border-white/15 p-8 text-center text-sm text-slate-400">该课程还没有章节，请在下方新增。</div>
          <article class="rounded-xl border border-sky-400/25 bg-sky-500/5 p-4"><h3 class="text-sm font-semibold text-sky-300">新增章节</h3><div class="mt-3 grid gap-3 md:grid-cols-[110px_1fr]"><input v-model.number="newChapter.chapter_no" type="number" min="1" class="input" placeholder="章节序号"><input v-model="newChapter.title" class="input" placeholder="章节名称（必填）"></div><textarea v-model="newChapter.notes" rows="2" class="input mt-3 !h-auto w-full resize-y" placeholder="章节备注（可选）"></textarea><div class="mt-3 flex justify-end"><button type="button" class="btn btn-primary" :disabled="savingId === 'new'" @click="createChapter">{{ savingId === 'new' ? '新增中…' : '新增章节' }}</button></div></article>
        </div>
      </section>
    </div>
    <div v-if="deleteTarget" class="chapter-confirm-backdrop" @click.self="deleteTarget = null">
      <section role="alertdialog" aria-modal="true" aria-labelledby="chapter-delete-title" class="chapter-confirm-dialog">
        <p class="text-xs font-semibold uppercase tracking-widest text-rose-400">危险操作</p>
        <h3 id="chapter-delete-title" class="mt-2 text-xl font-bold text-slate-50">确认删除“{{ deleteTarget.title }}”</h3>
        <p class="mt-2 text-sm leading-6 text-slate-400">删除章节可能同时删除其课时和关联数据。此操作不会因关闭窗口自动撤销。</p>
        <label class="mt-5 block text-xs text-slate-400">请输入完整章节名称进行确认
          <input v-model="deleteConfirmTitle" class="input mt-2 w-full" :placeholder="deleteTarget.title" autofocus @keyup.enter="confirmDelete">
        </label>
        <p v-if="deleteConfirmTitle && deleteConfirmTitle !== deleteTarget.title" class="mt-2 text-xs text-rose-400">输入内容与章节名称不一致。</p>
        <div class="mt-6 flex justify-end gap-2">
          <button type="button" class="btn btn-secondary" @click="deleteTarget = null">取消</button>
          <button type="button" class="btn chapter-delete-confirm" :disabled="deleteConfirmTitle !== deleteTarget.title || savingId === deleteTarget.id" @click="confirmDelete">{{ savingId === deleteTarget.id ? '删除中…' : '确认删除' }}</button>
        </div>
      </section>
    </div>
    </Teleport>
  </div>
</template>

<style scoped>
.chapter-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 20px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.58);
  backdrop-filter: blur(7px);
  -webkit-backdrop-filter: blur(7px);
}

.chapter-dialog {
  width: min(980px, 100%);
  max-height: calc(100vh - 40px);
  overflow: auto;
  border: 1px solid var(--line-strong);
  border-radius: var(--r-card);
  padding: 0 24px 24px;
  background: var(--surface-1);
  color: var(--txt-1);
  box-shadow: 0 28px 90px rgba(0, 0, 0, 0.42);
}

.chapter-dialog-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin: 0 -24px 20px;
  padding: 22px 24px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-1);
}

.chapter-dialog :deep(.input) {
  min-height: 38px;
  background-color: var(--surface-2);
  color: var(--txt-1);
}

.chapter-dialog :deep(textarea.input) {
  min-height: 72px;
}

.chapter-confirm-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1010;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.66);
}

.chapter-confirm-dialog {
  width: min(500px, 100%);
  border: 1px solid var(--acc-red-line);
  border-radius: var(--r-card);
  padding: 24px;
  background: var(--surface-1);
  color: var(--txt-1);
  box-shadow: 0 28px 90px rgba(0, 0, 0, 0.48);
}

.chapter-confirm-dialog :deep(.input) {
  min-height: 40px;
  background: var(--surface-2);
  color: var(--txt-1);
}

.chapter-delete-confirm {
  border-color: var(--acc-red);
  background: var(--acc-red);
  color: #fff;
}

.chapter-delete-confirm:not(:disabled):hover { filter: brightness(0.94); }

@media (max-width: 640px) {
  .chapter-backdrop { padding: 0; place-items: stretch; }
  .chapter-dialog { width: 100%; max-height: 100vh; border-radius: 0; padding: 0 15px 20px; }
  .chapter-dialog-header { margin: 0 -15px 16px; padding: 16px 15px 13px; }
  .chapter-confirm-dialog { padding: 20px; }
}
</style>
