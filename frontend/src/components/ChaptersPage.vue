<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChaptersApi, CoursesApi } from '../api/endpoints'
import type { Chapter, Course } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { chapterStatusLabel, chapterStatusTone, fmtDate, reviewStatusLabel } from '../utils/format'
import { useToast } from '../composables/useToast'

const courses = ref<Course[]>([])
const chapters = ref<Chapter[]>([])
const loading = ref(false)
const error = ref('')
const actingId = ref<number | null>(null)
const filterCourseId = ref<number | 'all'>('all')

const toast = useToast()

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cs, chs] = await Promise.all([CoursesApi.list(), ChaptersApi.list()])
    courses.value = cs
    chapters.value = chs
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

const courseMap = computed(() => {
  const m = new Map<number, Course>()
  courses.value.forEach(c => m.set(c.id, c))
  return m
})

const grouped = computed(() => {
  const map = new Map<number, Chapter[]>()
  chapters.value.forEach(ch => {
    if (!map.has(ch.course_id)) map.set(ch.course_id, [])
    map.get(ch.course_id)!.push(ch)
  })
  for (const list of map.values()) {
    list.sort((a, b) => (a.chapter_no ?? 9999) - (b.chapter_no ?? 9999))
  }
  return map
})

const filteredGroups = computed(() => {
  if (filterCourseId.value === 'all') return grouped.value
  const m = new Map<number, Chapter[]>()
  for (const [k, v] of grouped.value.entries()) {
    if (k === filterCourseId.value) m.set(k, v)
  }
  return m
})

const statusGroups = computed(() => {
  const counts: Record<string, number> = {}
  chapters.value.forEach(ch => {
    const s = (ch.status || 'unknown').toLowerCase()
    counts[s] = (counts[s] || 0) + 1
  })
  return counts
})

async function advance(ch: Chapter) {
  actingId.value = ch.id
  try {
    await ChaptersApi.advance(ch.id)
    toast.success(`已推进:${ch.title}`)
    await load()
  } catch (e: any) {
    toast.error(`推进失败:${e?.message || e}`)
  } finally {
    actingId.value = null
  }
}

async function markReview(ch: Chapter) {
  actingId.value = ch.id
  try {
    await ChaptersApi.markReview(ch.id)
    toast.success(`已标记章末复习:${ch.title}`)
    await load()
  } catch (e: any) {
    toast.error(`标记失败:${e?.message || e}`)
  } finally {
    actingId.value = null
  }
}

function courseName(id: number) {
  return courseMap.value.get(id)?.name || `课程 #${id}`
}

onMounted(load)
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="🗂️"
      title="章节进度"
      subtitle="按课程分组展示章节；支持按状态机推进、标记章末复习、生成复习材料。"
    >
      <template #actions>
        <select v-model="filterCourseId" class="input !w-48">
          <option value="all">全部课程 ({{ chapters.length }})</option>
          <option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
        <button class="btn btn-secondary" @click="load">刷新</button>
      </template>
    </PageHeader>

    <!-- 状态机分布 -->
    <section class="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
      <div v-for="(count, key) in statusGroups" :key="key" class="card-soft !p-3 text-center">
        <p class="text-xs text-slate-400">{{ chapterStatusLabel(String(key)) }}</p>
        <p class="text-xl font-bold text-slate-100">{{ count }}</p>
      </div>
    </section>

    <States :loading="loading" :error="error" :empty="!chapters.length" empty-icon="🗂️" empty-title="还没有章节数据">
      <div v-for="[cid, list] in filteredGroups" :key="cid" class="section">
        <div class="section-head">
          <div>
            <h3 class="card-title">{{ courseName(cid) }}</h3>
            <p class="card-muted">{{ list.length }} 个章节 · {{ courseMap.get(cid)?.teacher || '未指定教师' }}</p>
          </div>
        </div>
        <div class="space-y-2">
          <div
            v-for="ch in list"
            :key="ch.id"
            class="card-soft flex flex-wrap items-center justify-between gap-3"
          >
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-semibold text-slate-100">
                <span v-if="ch.chapter_no" class="mr-1 text-slate-400">第{{ ch.chapter_no }}章</span>
                {{ ch.title }}
              </p>
              <p class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                <StatusBadge :status="ch.status" :variant="chapterStatusTone(ch.status)">
                  {{ chapterStatusLabel(ch.status) }}
                </StatusBadge>
                <span>复习：{{ reviewStatusLabel(ch.review_status) }}</span>
                <span v-if="ch.completed_at">完成：{{ fmtDate(ch.completed_at) }}</span>
                <span v-if="ch.reviewed_at">已复习：{{ fmtDate(ch.reviewed_at) }}</span>
              </p>
            </div>
            <div class="flex gap-2">
              <button
                class="btn btn-primary !px-3 !py-1.5 text-xs"
                :disabled="actingId === ch.id"
                @click="advance(ch)"
              >
                {{ actingId === ch.id ? '处理中...' : '推进' }}
              </button>
              <button
                class="btn btn-secondary !px-3 !py-1.5 text-xs"
                :disabled="actingId === ch.id"
                @click="markReview(ch)"
              >
                {{ actingId === ch.id ? '处理中...' : '标记章末复习' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </States>
  </div>
</template>
