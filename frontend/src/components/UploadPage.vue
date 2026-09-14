<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CapabilitiesApi, ChaptersApi, CoursesApi, LessonsApi, MaterialsApi } from '../api/endpoints'
import type { Chapter, Course, Lesson } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import Icon from './widgets/Icon.vue'
import { useDataStore } from '../stores/data'
import { useToast } from '../composables/useToast'

interface UploadRecord {
  id?: number
  file: File
  state: 'pending' | 'uploading' | 'success' | 'failed'
  progress: number
  message?: string
  kind?: string
  path?: string
}

const dataStore = useDataStore()
const toast = useToast()

function kindLabel(r: any): string {
  if (r.kind && r.kind !== 'unknown') return r.kind
  const name = String(r.file?.name || r.filename || '').toLowerCase()
  if (name.endsWith('.pdf')) return 'PDF'
  if (name.endsWith('.pptx') || name.endsWith('.ppt')) return 'PPT'
  if (/\.(png|jpe?g|webp|gif|bmp)$/.test(name)) return '图片'
  if (name.endsWith('.docx') || name.endsWith('.doc')) return 'Word'
  if (name.endsWith('.md')) return 'Markdown'
  if (name.endsWith('.txt')) return '文本'
  return '未知类型'
}

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const lessons = ref<Lesson[]>([])
const loadingMeta = ref(false)

const courseId = ref<number | ''>('')
const chapterId = ref<number | ''>('')
const lessonId = ref<number | ''>('')

const uploads = ref<UploadRecord[]>([])
const dragOver = ref(false)
const globalError = ref('')
const maxUploadBytes = ref(50 * 1024 * 1024)

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function loadMeta() {
  loadingMeta.value = true
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
    globalError.value = e?.message || String(e)
  } finally {
    loadingMeta.value = false
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

function addFiles(files: FileList | null) {
  if (!files) return
  for (const f of Array.from(files)) {
    const tooLarge = f.size > maxUploadBytes.value
    uploads.value.push({
      file: f,
      state: tooLarge ? 'failed' : 'pending',
      progress: 0,
      message: tooLarge
        ? `文件为 ${fmtSize(f.size)}，超过 ${fmtSize(maxUploadBytes.value)} 上限；请先拆分或压缩。`
        : undefined,
    })
    if (tooLarge) toast.error(`${f.name} 超过 ${fmtSize(maxUploadBytes.value)} 上限`)
  }
}

function onFiles(e: Event) {
  const input = e.target as HTMLInputElement
  addFiles(input.files)
  input.value = ''
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  dragOver.value = false
  addFiles(e.dataTransfer?.files || null)
}

function removeRecord(idx: number) {
  uploads.value.splice(idx, 1)
}

async function startUpload() {
  globalError.value = ''
  if (!uploads.value.length) return
  for (const r of uploads.value) {
    if (r.state === 'success') continue
    if (r.file.size > maxUploadBytes.value) continue
    r.state = 'uploading'
    r.progress = 10
    const form = new FormData()
    form.append('file', r.file)
    if (courseId.value) form.append('course_id', String(courseId.value))
    if (chapterId.value) form.append('chapter_id', String(chapterId.value))
    if (lessonId.value) form.append('lesson_id', String(lessonId.value))
    try {
      r.progress = 40
      const result = await MaterialsApi.upload(form)
      r.id = result.id
      r.kind = result.kind
      r.state = 'success'
      r.progress = 100
      r.message = `已入库 #${result.id}（${result.kind || 'unknown'}）`
      toast.success(`已入库 #${result.id}（${kindLabel({ ...r, kind: result.kind })}）`)
    } catch (e: any) {
      r.state = 'failed'
      r.message = e?.message || String(e)
      toast.error(`${r.file?.name} 上传失败:${e?.message || e}`)
    }
  }
  await dataStore.ensureCourses(true)
}

function resetAll() {
  uploads.value = []
}

/** 返回 icons.ts 中的图标名，由 <Icon> 渲染 */
function fileIcon(name: string): string {
  const lower = name.toLowerCase()
  if (lower.endsWith('.pdf')) return 'file-text'
  if (lower.endsWith('.pptx') || lower.endsWith('.ppt')) return 'chart-bar'
  if (lower.endsWith('.docx') || lower.endsWith('.doc')) return 'file'
  if (/\.(png|jpe?g|webp|gif|bmp)$/i.test(lower)) return 'file-image'
  if (lower.endsWith('.md') || lower.endsWith('.txt')) return 'file-text'
  if (/\.(mp3|wav|m4a|aac|ogg|flac)$/i.test(lower)) return 'music'
  return 'package'
}

onMounted(async () => {
  await Promise.all([
    loadMeta(),
    CapabilitiesApi.limits().then(v => { maxUploadBytes.value = v.max_upload_bytes }).catch(() => {}),
  ])
  await loadMaterials()
})

const materials = ref<Record<string, any>[]>([])
const materialsLoading = ref(false)

async function loadMaterials() {
  materialsLoading.value = true
  try {
    materials.value = await MaterialsApi.list()
  } catch (e: any) {
    materials.value = []
  } finally {
    materialsLoading.value = false
  }
}

function statusColor(s?: string): 'green' | 'red' | 'blue' | 'amber' | 'slate' {
  const st = (s || '').toLowerCase()
  if (['ready', 'synced'].includes(st)) return 'green'
  if (['failed'].includes(st)) return 'red'
  if (['indexed'].includes(st)) return 'blue'
  if (['uploaded', 'queued', 'parsing', 'pending'].includes(st)) return 'amber'
  return 'slate'
}

async function retryMaterial(id: number) {
  try {
    await MaterialsApi.retry(id)
    toast.success('已重新入队解析')
    loadMaterials()
  } catch (e: any) {
    toast.error(e?.message || '重试失败')
  }
}

async function removeMaterial(id: number) {
  try {
    await MaterialsApi.remove(id)
    toast.success(`已删除材料 #${id}`)
    loadMaterials()
  } catch (e: any) {
    toast.error(e?.message || '删除失败')
  }
}
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="inbox"
      title="材料收件箱"
      subtitle="上传材料进入收件箱，自动解析并生成检索块；可绑定课程 / 章节 / 课时。"
    >
      <template #actions>
        <button class="btn btn-secondary" :disabled="!uploads.length" @click="resetAll">清空队列</button>
      </template>
    </PageHeader>

    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">绑定上下文（可选）</h3>
          <p class="card-muted">不绑定也能上传成功；绑定后听课 / 作业流可直接引用。</p>
        </div>
      </div>
      <States :loading="loadingMeta" :error="globalError">
        <div class="grid gap-3 md:grid-cols-3">
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
        </div>
      </States>
    </section>

    <section
      class="section"
      :class="dragOver ? 'ring-2 ring-blue-500/60' : ''"
      @dragover.prevent="dragOver = true"
      @dragleave="dragOver = false"
      @drop="onDrop"
    >
      <div class="section-head">
        <div>
          <h3 class="card-title">上传队列</h3>
          <p class="card-muted">点击选择文件或拖拽到下方区域，全部完成后会给出结果摘要。</p>
        </div>
        <div class="flex gap-2">
          <label class="btn btn-primary cursor-pointer">
            选择文件
            <input type="file" multiple accept=".pdf,.ppt,.pptx,.doc,.docx,.md,.txt,.srt,.vtt,.json,.png,.jpg,.jpeg,.webp,.gif,.mp3,.m4a,.wav" class="hidden" @change="onFiles" />
          </label>
          <button class="btn btn-secondary" :disabled="!uploads.length" @click="startUpload">开始上传</button>
        </div>
      </div>

      <States :empty="!uploads.length" empty-icon="inbox" empty-title="还没有待上传文件" empty-hint="点击右上角选择文件或拖入此区域。">
        <ul class="divide-y divide-slate-800 rounded-xl border border-slate-800 bg-slate-950/40">
          <li v-for="(r, idx) in uploads" :key="idx" class="flex items-center gap-3 px-4 py-3">
            <span class="grid h-9 w-9 shrink-0 place-items-center rounded-[10px] border border-white/10 bg-white/[.04] text-t2">
              <Icon :name="fileIcon(r.file.name)" :size="17" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-slate-100">{{ r.file.name }}</p>
              <p class="text-[11px] text-slate-500">
                {{ (r.file.size / 1024).toFixed(1) }} KB
                <span v-if="r.kind"> · 类型：{{ r.kind }}</span>
              </p>
              <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                <div
                  class="h-full transition-all"
                  :class="r.state === 'failed' ? 'bg-rose-500' : r.state === 'success' ? 'bg-emerald-500' : 'bg-blue-500'"
                  :style="{ width: `${r.progress}%` }"
                />
              </div>
              <p v-if="r.message" class="mt-1 text-[11px]"
                 :class="r.state === 'failed' ? 'text-[var(--acc-red)]' : 'text-[var(--acc-green)]'">
                {{ r.message }}
              </p>
            </div>
            <div class="flex flex-col items-end gap-2">
              <StatusBadge
                :status="r.state"
                :variant="r.state === 'success' ? 'green' : r.state === 'failed' ? 'red' : 'amber'"
              >
                {{ r.state }}
              </StatusBadge>
              <button v-if="r.state !== 'uploading'" class="btn btn-ghost !px-2 !py-1 text-xs" @click="removeRecord(idx)">移除</button>
            </div>
          </li>
        </ul>
      </States>
    </section>

    <section class="section">
      <div class="section-head">
        <div>
          <h3 class="card-title">材料库</h3>
          <p class="card-muted">已上传材料的解析状态与绑定关系。</p>
        </div>
        <button class="btn btn-secondary btn-sm" @click="loadMaterials">刷新</button>
      </div>
      <States :loading="materialsLoading" :empty="!materials.length" empty-icon="layers" empty-title="暂无材料"
        empty-hint="上传文件后这里会显示解析状态。">
        <div class="overflow-x-auto rounded-xl border border-slate-800">
          <table class="w-full text-left text-sm">
            <thead class="bg-slate-900/60 text-xs uppercase text-slate-400">
              <tr>
                <th class="px-4 py-2">名称</th>
                <th class="px-4 py-2">类型</th>
                <th class="px-4 py-2">状态</th>
                <th class="px-4 py-2">大小</th>
                <th class="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              <tr v-for="m in materials" :key="m.id">
                <td class="px-4 py-2 text-slate-100">{{ m.name || m.display_name || `材料 #${m.id}` }}</td>
                <td class="px-4 py-2 capitalize text-slate-400">{{ m.kind || m.type || 'text' }}</td>
                <td class="px-4 py-2">
                  <StatusBadge :status="m.status || m.parser_status || 'pending'" :variant="statusColor(m.status || m.parser_status)">
                    {{ m.status || m.parser_status || 'pending' }}
                  </StatusBadge>
                  <span v-if="m.parse_error" class="block text-[11px] text-[var(--acc-red)]">{{ m.parse_error }}</span>
                </td>
                <td class="px-4 py-2 text-slate-400">
                  {{ m.size_bytes ? (m.size_bytes / 1024).toFixed(1) + ' KB' : '—' }}
                </td>
                <td class="px-4 py-2 text-right">
                  <div class="inline-flex gap-1">
                    <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="retryMaterial(m.id)"
                      :disabled="m.status === 'parsing' || m.status === 'queued'">重试</button>
                    <button class="btn btn-ghost !px-2 !py-1 text-xs text-[var(--acc-red)]" @click="removeMaterial(m.id)">删除</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </States>
    </section>
  </div>
</template>
