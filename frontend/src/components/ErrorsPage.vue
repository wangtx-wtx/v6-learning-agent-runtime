<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ErrorsApi } from '../api/endpoints'
import type { ErrorItem } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { fmtRelative, safeJson, statusLabel, truncate } from '../utils/format'
import { useToast } from '../composables/useToast'

const toast = useToast()

const filters = [
  { key: 'provisional', label: '待确认', tone: 'purple' as const },
  { key: 'confirmed', label: '已确认', tone: 'green' as const },
  { key: 'rejected', label: '已拒绝', tone: 'red' as const },
]

const filter = ref<string>('provisional')
const errors = ref<ErrorItem[]>([])
const loading = ref(false)
const error = ref('')
const actingId = ref<number | null>(null)
const expanded = ref<Record<number, boolean>>({})

async function load() {
  loading.value = true
  error.value = ''
  try {
    errors.value = await ErrorsApi.list({ status: filter.value })
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

watch(filter, load)
onMounted(load)

async function confirmItem(e: ErrorItem) {
  actingId.value = e.id
  try {
    await ErrorsApi.confirm(e.id)
    toast.success(`错题 #${e.id} 已确认`)
    await load()
  } catch (err: any) {
    toast.error(`确认失败:${err?.message || err}`)
  } finally {
    actingId.value = null
  }
}

async function rejectItem(e: ErrorItem) {
  actingId.value = e.id
  try {
    await ErrorsApi.reject(e.id)
    toast.success(`错题 #${e.id} 已拒绝`)
    await load()
  } catch (err: any) {
    toast.error(`拒绝失败:${err?.message || err}`)
  } finally {
    actingId.value = null
  }
}

interface AiError { root_cause?: string; knowledge_points?: string[]; candidates?: string[] }
interface FinalError { root_cause?: string; knowledge_points?: string[] }

function ai(e: ErrorItem): AiError {
  return safeJson<AiError>(e.ai_error_json, {}) || {}
}
function final(e: ErrorItem): FinalError {
  return safeJson<FinalError>(e.final_error_json, {}) || {}
}

function toggle(id: number) {
  expanded.value[id] = !expanded.value[id]
}
</script>

<template>
  <div class="page-shell">
    <PageHeader
      icon="circle-x"
      title="错题确认"
      subtitle="Provisional → Confirmed / Rejected 三态流转；展示 AI 候选错因、相关知识点、用户解释。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="load">刷新</button>
      </template>
    </PageHeader>

    <section class="section">
      <div class="flex flex-wrap items-center gap-2">
        <button
          v-for="f in filters"
          :key="f.key"
          class="btn"
          :class="filter === f.key ? 'btn-primary' : 'btn-secondary'"
          @click="filter = f.key"
        >
          {{ f.label }}
        </button>
        <span class="ml-auto text-xs text-slate-500">
          当前筛选:{{ filters.find(x => x.key === filter)?.label }} · 共 {{ errors.length }} 条
        </span>
      </div>
    </section>

    <States
      :loading="loading"
      :error="error"
      :empty="!errors.length"
      empty-icon="circle-x"
      :empty-title="filter === 'provisional' ? '没有待确认错题' : filter === 'confirmed' ? '没有已确认错题' : '没有已拒绝错题'"
      :empty-hint="filter === 'provisional' ? '运行作业流后会自动写入 provisional 错题。' : '可在左侧切换筛选。'"
    >
      <article v-for="e in errors" :key="e.id" class="card space-y-3">
        <header class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-sm font-semibold text-slate-100">错题 #{{ e.id }}</p>
            <p class="mt-0.5 text-[11px] text-slate-500">
              <span v-if="e.course_id">课程 #{{ e.course_id }}</span>
              <span v-if="e.chapter_id"> · 章节 #{{ e.chapter_id }}</span>
              <span v-if="e.lesson_id"> · 课时 #{{ e.lesson_id }}</span>
              <span v-if="e.next_review_at"> · 下次复习:{{ fmtRelative(e.next_review_at) }}</span>
            </p>
          </div>
          <StatusBadge :status="e.status">{{ statusLabel(e.status) }}</StatusBadge>
        </header>

        <div>
          <p class="text-xs font-medium text-slate-400">题目</p>
          <p class="mt-1 whitespace-pre-line text-sm text-slate-100">
            {{ e.question_text || '（无题目文本）' }}
          </p>
        </div>

        <div class="grid gap-3 md:grid-cols-2">
          <div>
            <p class="text-xs font-medium text-slate-400">学生答案</p>
            <p class="mt-1 whitespace-pre-line text-sm text-[var(--acc-red)]">
              {{ e.student_answer || '（无）' }}
            </p>
          </div>
          <div>
            <p class="text-xs font-medium text-slate-400">正确答案</p>
            <p class="mt-1 whitespace-pre-line text-sm text-[var(--acc-green)]">
              {{ e.correct_answer || '（未填）' }}
            </p>
          </div>
        </div>

        <button
          type="button"
          class="flex w-full items-center justify-between rounded-lg bg-slate-950/60 px-3 py-2 text-left text-xs text-slate-400 hover:text-slate-200"
          @click="toggle(e.id)"
        >
          <span>候选错因 / 知识点 / 用户解释</span>
          <span>{{ expanded[e.id] ? '收起' : '展开' }}</span>
        </button>
        <div v-if="expanded[e.id]" class="space-y-2 rounded-lg bg-slate-950/60 p-3 text-xs text-slate-300">
          <div>
            <p class="text-slate-500">AI 候选错因：</p>
            <p v-if="ai(e).root_cause" class="text-slate-200">{{ ai(e).root_cause }}</p>
            <p v-else class="text-slate-500">（未给出）</p>
          </div>
          <div>
            <p class="text-slate-500">候选错因列表：</p>
            <ul v-if="ai(e).candidates?.length" class="mt-1 list-disc pl-5 text-slate-200">
              <li v-for="(c, i) in ai(e).candidates" :key="i">{{ c }}</li>
            </ul>
            <p v-else class="text-slate-500">（无）</p>
          </div>
          <div>
            <p class="text-slate-500">相关知识点：</p>
            <ul v-if="ai(e).knowledge_points?.length" class="mt-1 list-disc pl-5 text-slate-200">
              <li v-for="(k, i) in ai(e).knowledge_points" :key="i">{{ k }}</li>
            </ul>
            <p v-else class="text-slate-500">（无）</p>
          </div>
          <div>
            <p class="text-slate-500">最终错因（人工 / 二次确认）：</p>
            <p v-if="final(e).root_cause" class="text-slate-200">{{ final(e).root_cause }}</p>
            <p v-else class="text-slate-500">（未确认）</p>
          </div>
          <div>
            <p class="text-slate-500">用户解释：</p>
            <p class="whitespace-pre-line text-slate-200">{{ e.user_explanation || '（无）' }}</p>
          </div>
          <div v-if="e.image_file">
            <p class="text-slate-500">图片：</p>
            <p class="font-mono text-slate-300">{{ truncate(e.image_file, 200) }}</p>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            class="btn btn-primary !px-3 !py-1.5 text-xs"
            :disabled="actingId === e.id || (e.status || '').toLowerCase() === 'confirmed'"
            @click="confirmItem(e)"
          >
            {{ actingId === e.id ? '处理中...' : '确认' }}
          </button>
          <button
            class="btn btn-secondary !px-3 !py-1.5 text-xs"
            :disabled="actingId === e.id || (e.status || '').toLowerCase() === 'rejected'"
            @click="rejectItem(e)"
          >
            {{ actingId === e.id ? '处理中...' : '拒绝' }}
          </button>
          <span class="ml-auto text-[11px] text-slate-500">状态:{{ statusLabel(e.status) }}</span>
        </div>
      </article>
    </States>
  </div>
</template>