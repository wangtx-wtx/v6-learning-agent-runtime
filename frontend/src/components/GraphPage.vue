<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
// ECharts 按需引入（方案 13.3）：只打包图类型/组件/渲染器，单独分包
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { GraphEdge, GraphNode } from '../api/endpoints'
import { GraphApi } from '../api/endpoints'
import PageHeader from './widgets/PageHeader.vue'
import States from './widgets/States.vue'
import StatusBadge from './widgets/StatusBadge.vue'
import { graphTypeLabel, prettyJson } from '../utils/format'

echarts.use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer])

const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const loading = ref(false)
const error = ref('')
const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.EChartsType | null = null
const selected = ref<GraphNode | null>(null)

const TYPE_COLOR: Record<string, string> = {
  course: '#3b82f6',
  chapter: '#10b981',
  lesson: '#f59e0b',
  knowledge: '#f43f5e',
  note: '#8b5cf6',
  error: '#ef4444',
  default: '#64748b',
}

const TYPE_LABEL: Record<string, string> = {
  course: '课程',
  chapter: '章节',
  lesson: '课时',
  knowledge: '知识点',
  note: '笔记',
  error: '错题',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await GraphApi.get()
    nodes.value = r.nodes || []
    edges.value = r.edges || []
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
  await nextTick()
  renderChart()
}

function buildOption() {
  const data = nodes.value.map(n => {
    const type = (n.node_type || 'default').toLowerCase()
    const knownType = (type in TYPE_COLOR) ? type : 'default'
    const symbolSize =
      knownType === 'course' ? 60 :
      knownType === 'chapter' ? 44 :
      knownType === 'lesson' ? 32 :
      knownType === 'knowledge' ? 22 :
      knownType === 'note' ? 24 :
      knownType === 'error' ? 26 :
      22
    return {
      id: String(n.id),
      name: n.title || `Node ${n.id}`,
      symbolSize,
      itemStyle: { color: TYPE_COLOR[knownType] },
      node_type: knownType,
      category: knownType,
      value: n,
      label: { show: symbolSize >= 32, color: '#cbd5e1', fontSize: 11 },
    }
  })
  const links = edges.value.map(e => ({
    source: String(e.source_id),
    target: String(e.target_id),
    lineStyle: { color: '#475569', width: 1, opacity: 0.6 },
    relation: e.relation,
  }))
  // 真实存在的图例项:仅保留 nodes 中实际命中的 type
  const usedTypes = Array.from(new Set(data.map(d => d.category as string)))
  const categories = Object.entries(TYPE_LABEL)
    .filter(([k]) => usedTypes.includes(k))
    .map(([k, v]) => ({ name: v, id: k, itemStyle: { color: TYPE_COLOR[k] } }))
  const legendData = categories.map(c => ({ name: c.name, icon: 'circle' }))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      formatter: (params: any) => {
        if (params.dataType === 'node') {
          const n = params.data?.value as GraphNode
          return `<b>${n.title}</b><br/>类型：${TYPE_LABEL[n.node_type] || n.node_type || '默认'}<br/>ID: ${n.id}`
        }
        if (params.dataType === 'edge') {
          return `${params.data?.relation || '关联'}`
        }
        return ''
      },
    },
    legend: {
      data: legendData,
      textStyle: { color: '#cbd5e1', fontSize: 11 },
      top: 8,
      itemWidth: 10,
      itemHeight: 10,
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      categories,
      data,
      links,
      force: { repulsion: 220, edgeLength: 90, gravity: 0.05 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 2 } },
      lineStyle: { color: '#475569', width: 1 },
      edgeSymbol: ['none', 'none'],
    }],
  }
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value, undefined, { renderer: 'canvas' })
  chart.setOption(buildOption(), true)
  chart.off('click')
  chart.on('click', { dataType: 'node' }, (params: any) => {
    selected.value = params.data?.value || null
  })
}

function resize() {
  chart?.resize()
}

onMounted(async () => {
  await load()
  window.addEventListener('resize', resize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})

watch(nodes, () => renderChart())

const stats = computed(() => {
  const byType: Record<string, number> = {}
  nodes.value.forEach(n => {
    const t = (n.node_type || 'default').toLowerCase()
    byType[t] = (byType[t] || 0) + 1
  })
  return byType
})

const nodeTypes = computed(() => Object.keys(stats.value))
const hasEdges = computed(() => edges.value.length > 0)
const isolatedNodes = computed(() => !hasEdges.value && nodes.value.length > 0)

function fitGraph() {
  if (!chart) return
  chart.dispatchAction({ type: 'graphRoam', zoom: 1 })
}
</script>

<template>
  <div class="page-shell">
    <PageHeader
      emoji="🕸️"
      title="知识图谱"
      subtitle="课程 → 章节 → 课时 → 知识点 → 笔记 / 错题 的层级关系，支持拖拽、缩放、点击查看详情。"
    >
      <template #actions>
        <button class="btn btn-secondary" @click="fitGraph">重置视图</button>
        <button class="btn btn-secondary" @click="load">刷新</button>
      </template>
    </PageHeader>

    <States :loading="loading" :error="error" :empty="!nodes.length" empty-icon="🕸️"
            empty-title="暂无图谱数据"
            empty-hint="可通过数据迁移 (POST /api/migrate) 把旧版数据导入，或在听课 / 作业 / 复习流运行时自动生成节点。">
      <section class="grid gap-4 xl:grid-cols-[1fr_320px]">
        <div class="section">
          <div class="section-head">
            <div>
              <h3 class="card-title">图谱视图</h3>
              <p class="card-muted">节点 {{ nodes.length }} · 边 {{ edges.length }} · 力导向布局</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <StatusBadge v-for="type in nodeTypes" :key="type" :status="type" variant="blue">
                {{ graphTypeLabel(type) }} {{ stats[type] }}
              </StatusBadge>
            </div>
          </div>
          <div v-if="isolatedNodes" class="mb-3 rounded-xl border border-amber-700/40 bg-amber-950/30 px-4 py-3 text-xs text-amber-200">
            <p class="font-semibold">⚠ 当前图谱仅有 {{ nodes.length }} 个节点、{{ edges.length }} 条边,无任何关联关系。</p>
            <p class="mt-1 text-amber-300/80">可能的修复:运行 1 次完整的听课 / 作业 / 复习流水线以自动生成节点关系,或在后端调用 <code class="rounded bg-amber-900/50 px-1.5 py-0.5 font-mono">POST /api/migrate</code> 重建图谱。</p>
          </div>
          <div ref="chartRef" class="h-[560px] w-full rounded-xl bg-slate-950/40" />
        </div>

        <div class="section">
          <div class="section-head">
            <div>
              <h3 class="card-title">节点详情</h3>
              <p class="card-muted">点击图谱中的节点查看。</p>
            </div>
          </div>
          <div v-if="selected" class="space-y-2 text-sm text-slate-200">
            <p class="text-base font-semibold">{{ selected.title }}</p>
            <p class="text-[11px] text-slate-500">ID: {{ selected.id }} · 课程 #{{ selected.course_id || '—' }}</p>
            <p class="text-xs text-slate-400">类型：{{ graphTypeLabel(selected.node_type) }}</p>
            <pre v-if="selected.meta_json" class="code-panel max-h-60 whitespace-pre-wrap">{{ prettyJson(selected.meta_json) }}</pre>
            <button class="btn btn-ghost !px-2 !py-1 text-xs" @click="selected = null">关闭</button>
          </div>
          <p v-else class="text-xs text-slate-500">尚未选择节点。</p>

          <div class="mt-4 border-t border-slate-800 pt-3">
            <p class="text-xs text-slate-400">节点类型图例：</p>
            <ul class="mt-2 space-y-1 text-xs text-slate-300">
              <li v-for="(label, key) in TYPE_LABEL" :key="key" class="flex items-center gap-2">
                <span class="h-3 w-3 rounded-full" :style="{ background: TYPE_COLOR[key] }" />
                {{ label }}
              </li>
            </ul>
          </div>
        </div>
      </section>
    </States>
  </div>
</template>
