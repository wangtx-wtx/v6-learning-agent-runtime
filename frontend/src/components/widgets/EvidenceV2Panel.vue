<script setup lang="ts">
/**
 * V6 Phase 4：Evidence V2 只读审计面板。
 *
 * 显示内容（全部来自 `/api/runs/{id}/evidence-v2`）:
 *  - Evidence gate + 关键 claim 证据率（含分母，明确「分母为 0」不等于验证了很多证据）
 *  - invalid / cross-domain / unbound / unread 来源计数
 *  - 三类 claim 分组：classroom_fact / classroom_paraphrase / ai_explanation
 *  - 点击 Source ID 查看数据库原文与 locator
 *  - ai_explanation 明确显示「模型教学补充（非课堂原话）」
 *
 * 所有按钮 `type="button"`，不触发页面刷新。
 */
import { computed, ref } from 'vue'
import type { EvidenceV2Claim, EvidenceV2View } from '../../api/endpoints'

const props = defineProps<{
  view: EvidenceV2View | null
  label: string
  busy?: boolean
  spanText: (sourceId: string) => string | null
}>()

const emit = defineEmits<{ (e: 'refresh'): void; (e: 'open-span', sourceId: string): void }>()

const CLAIM_TYPE_ORDER = ['classroom_fact', 'classroom_paraphrase', 'ai_explanation'] as const
const CLAIM_TYPE_LABEL: Record<string, string> = {
  classroom_fact: '课堂事实',
  classroom_paraphrase: '课堂重述',
  ai_explanation: '模型教学补充',
}
const IMPORTANCE_LABEL: Record<string, string> = {
  critical: '关键',
  major: '重要',
  supporting: '辅助',
}
const BINDING_LABEL: Record<string, string> = {
  bound: '已绑定',
  invalid: '引用非法',
  missing: '来源不存在',
  not_required: '无需来源',
}
/** Phase 4.1：来源相对「本 claim 的 producer 调用」的可见性。 */
const VISIBILITY_LABEL: Record<string, string> = {
  visible: '已进入本 claim 的模型输入',
  not_visible: '未进入本 claim 的模型输入',
  unknown: '可见性未知（缺 provenance）',
}
const STATUS_LABEL: Record<string, string> = {
  passed: '通过',
  failed: '未通过',
  not_required: '无需证据',
  pending: '待处理',
}
const ORIGIN_LABEL: Record<string, string> = {
  classroom_fact: '课堂事实（教师原话 / 材料直接支持）',
  classroom_paraphrase: '课堂重述（绑定存在，语义等价属 Phase 5 Critic）',
  ai_explanation: '模型教学补充（非课堂原话）',
}

const report = computed(() => props.view?.report || null)
const expandedClaimId = ref<number | null>(null)

const grouped = computed(() => {
  const claims = props.view?.claims || []
  const groups: Record<string, EvidenceV2Claim[]> = {}
  for (const claim of claims) {
    const key = claim.claim_type || 'other'
    if (!groups[key]) groups[key] = []
    groups[key].push(claim)
  }
  const order = [...CLAIM_TYPE_ORDER, ...Object.keys(groups).filter(
    k => !CLAIM_TYPE_ORDER.includes(k as any))]
  return order.filter(t => groups[t]?.length).map(t => ({
    type: t,
    label: CLAIM_TYPE_LABEL[t] || t,
    note: ORIGIN_LABEL[t] || '',
    claims: groups[t],
  }))
})

function gateClass(gate?: string | null) {
  if (gate === 'passed') return 'chip-green'
  if (gate === 'failed') return 'chip-red'
  return 'chip-amber'
}
function bindingClass(status: string) {
  if (status === 'bound') return 'chip-green'
  if (status === 'not_required') return 'chip'
  return 'chip-red'
}
function visibilityClass(visibility: string) {
  if (visibility === 'visible') return 'chip-green'
  if (visibility === 'not_visible') return 'chip-red'
  return 'chip-amber'
}
function pct(value: unknown) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(1)}%`
}
function num(value: unknown) {
  const n = Number(value)
  return Number.isFinite(n) ? String(n) : '—'
}
function fmtMs(ms: number | null) {
  if (ms == null) return null
  const total = Math.max(0, Math.round(ms / 1000))
  const h = String(Math.floor(total / 3600)).padStart(2, '0')
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const s = String(total % 60).padStart(2, '0')
  return `${h}:${m}:${s}`
}
function locatorText(src: { locator: string | null; start_ms: number | null; end_ms: number | null;
                            page_no: number | null; slide_no: number | null }) {
  const parts: string[] = []
  if (src.locator) parts.push(src.locator)
  const start = fmtMs(src.start_ms)
  const end = fmtMs(src.end_ms)
  if (start) parts.push(end ? `${start}–${end}` : start)
  if (src.page_no != null) parts.push(`第 ${src.page_no} 页`)
  if (src.slide_no != null) parts.push(`第 ${src.slide_no} 张`)
  return parts.join(' · ') || '—'
}
function toggleClaim(id: number) {
  expandedClaimId.value = expandedClaimId.value === id ? null : id
}
</script>

<template>
  <section class="section">
    <div class="section-head">
      <div>
        <h3 class="card-title">证据绑定（V6 Phase 4 · Evidence V2）</h3>
        <p class="card-muted">
          模型只选 Source ID；<b>原文一律由程序从数据库读取并绑定</b>（bound_quote 不是模型生成的）。
          Binder 只验证「引用存在 / 归属正确 / quote 来自数据库 / 类型与证据要求一致」，
          不声称已完成语义评审。<b>不进入最终笔记</b>（Composer V2 属 Phase 5）。
        </p>
      </div>
      <button type="button" class="btn btn-secondary" :disabled="busy" @click="emit('refresh')">
        刷新证据
      </button>
    </div>

    <template v-if="view && report">
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div class="card-soft">
          <p class="stat-label">Evidence gate</p>
          <p class="stat-value">
            <span class="chip" :class="gateClass(report.gate)">{{ report.gate }}</span>
          </p>
          <p class="card-muted">{{ report.degradation_reason || '无阻断原因' }}</p>
        </div>
        <div class="card-soft">
          <p class="stat-label">关键 claim 证据率</p>
          <p class="stat-value">{{ pct(report.critical_claim_evidence_rate) }}</p>
          <p class="card-muted">
            {{ num(report.critical_claims_bound) }} / {{ num(report.critical_evidence_denominator) }}
            <span v-if="report.critical_evidence_denominator === 0">
              （分母为 0：本轮没有需要课堂证据的关键 claim，不代表验证了很多证据）
            </span>
          </p>
        </div>
        <div class="card-soft">
          <p class="stat-label">claim 总数 / 需要证据</p>
          <p class="stat-value">{{ num(report.claims_total) }} / {{ num(report.required_claims) }}</p>
          <p class="card-muted">
            已绑定 {{ num(report.bound_claims) }} · 失败 {{ num(report.failed_claims) }} ·
            无需证据 {{ num(report.not_required_claims) }}
          </p>
        </div>
        <div class="card-soft">
          <p class="stat-label">非法 / 跨域 / 缺失 / 未进入本 claim 生产者</p>
          <p class="stat-value">
            {{ num(report.invalid_source_refs) }} / {{ num(report.cross_domain_refs) }} /
            {{ num(report.unbound_source_refs) }} / {{ num(report.not_in_claim_producer_refs) }}
          </p>
          <p class="card-muted">
            缺少 producer provenance 的 claim {{ num(report.claims_without_provenance) }} 条 ·
            legacy evidence_links 兼容投影 {{ num(report.legacy_projection_rows) }} 行
          </p>
        </div>
      </div>

      <p class="mt-3 rounded-lg px-3 py-2 text-xs"
         style="background: var(--surface-inset); color: var(--txt-3)">
        可见性口径：<b>逐 claim、逐生产者调用</b>（{{ view.evidence_semantics.visibility_scope || 'claim_producer_invocations' }}）。
        {{ view.evidence_semantics.visibility_note || '「数据库里存在」不等于「生成该主张的模型读过」。' }}
      </p>

      <p v-if="report.issues.length" class="mt-3 rounded-lg px-3 py-2 text-xs"
         style="background: var(--surface-inset); color: var(--txt-3)">
        {{ report.issues.join('；') }}
      </p>

      <p v-if="!view.claims.length" class="mt-3 rounded-lg px-3 py-2 text-xs"
         style="background: var(--surface-inset); color: var(--txt-2)">
        本轮没有任何 claim（理解层与认知层都未产出可投影内容）。
      </p>

      <div v-for="group in grouped" :key="group.type" class="mt-4">
        <p class="stat-label">
          {{ group.label }}（{{ group.claims.length }}）
          <span class="card-muted">· {{ group.type }} · {{ group.note }}</span>
        </p>
        <table class="data-table mt-2">
          <thead>
            <tr>
              <th>claim</th><th>重要程度</th><th>证据状态</th><th>来源</th><th>绑定详情</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="claim in group.claims" :key="claim.claim_id">
              <tr>
                <td>
                  <b>{{ claim.claim_text || '—' }}</b>
                  <p class="card-muted">{{ claim.producer_node || '—' }}</p>
                </td>
                <td>
                  <span class="chip" :class="claim.importance === 'critical' ? 'chip-red' : 'chip'">
                    {{ IMPORTANCE_LABEL[claim.importance] || claim.importance }}
                  </span>
                </td>
                <td>
                  <span class="chip" :class="claim.evidence_status === 'passed' ? 'chip-green'
                    : (claim.evidence_status === 'not_required' ? 'chip' : 'chip-red')">
                    {{ STATUS_LABEL[claim.evidence_status] || claim.evidence_status }}
                  </span>
                  <p v-if="claim.is_ai_explanation" class="card-muted">{{ view.ai_explanation_label }}</p>
                </td>
                <td>
                  <button v-for="src in claim.sources" :key="src.source_id" type="button"
                          class="btn btn-ghost !px-2 !py-1 text-xs"
                          @click="emit('open-span', src.source_id)">{{ src.source_id }}</button>
                  <span v-if="!claim.sources.length" class="card-muted">—</span>
                </td>
                <td>
                  <button type="button" class="btn btn-ghost !px-2 !py-1 text-xs"
                          @click="toggleClaim(claim.claim_id)">
                    {{ expandedClaimId === claim.claim_id ? '收起' : '展开' }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedClaimId === claim.claim_id">
                <td colspan="5">
                  <div class="grid gap-2">
                    <div v-for="src in claim.sources" :key="src.source_id"
                         class="card-soft">
                      <p class="stat-label">
                        {{ src.source_id }}
                        <span class="chip" :class="bindingClass(src.binding_status)">
                          {{ BINDING_LABEL[src.binding_status] || src.binding_status }}
                        </span>
                        <span class="chip">{{ src.binding_method }}</span>
                        <span class="chip" :class="visibilityClass(src.visibility)">
                          {{ VISIBILITY_LABEL[src.visibility] || src.visibility }}
                        </span>
                      </p>
                      <p class="card-muted">定位：{{ locatorText(src) }}</p>
                      <p v-if="src.visibility === 'not_visible' && claim.requires_source"
                         class="text-xs" style="color: var(--acc-red)">
                        该来源<b>未进入生成此主张的模型输入</b>
                        <span v-if="claim.provenance?.invocation_refs?.length">
                          （本条 claim 的生产调用：{{ claim.provenance.invocation_refs.join('、') }}）
                        </span>
                        —— 其他批次读过不构成证据。
                      </p>
                      <p v-if="!claim.provenance?.available" class="text-xs"
                         style="color: var(--acc-red)">
                        缺少 producer provenance：无法证明该来源进入过任何相关模型调用，
                        因此按未通过处理。
                      </p>
                      <p v-if="src.visible_invocations?.length" class="card-muted">
                        真正发过该来源的调用：{{ src.visible_invocations.join('、') }}
                      </p>
                      <p v-if="src.failure_reason" class="text-xs" style="color: var(--acc-red)">
                        失败原因：{{ src.failure_reason }}
                      </p>
                      <p class="stat-label mt-2">数据库原文（bound_quote）</p>
                      <p class="content">{{ src.bound_quote || '（无原文：未绑定）' }}</p>
                      <p class="card-muted">quote_hash {{ src.quote_hash || '—' }}</p>
                      <p v-if="spanText(src.source_id)" class="card-muted">
                        实时读取：{{ spanText(src.source_id) }}
                      </p>
                    </div>
                    <p class="card-muted">
                      producer：{{ claim.provenance?.producer_node || '—' }} ·
                      类型 {{ claim.provenance?.producer_kind || '—' }} ·
                      依据 {{ claim.provenance?.visibility_basis || '—' }} ·
                      调用 {{ (claim.provenance?.invocation_refs || []).join('、') || '—' }}
                      <span v-if="claim.provenance?.source_scope">
                        （可见性范围 {{ claim.provenance.source_scope }}）
                      </span>
                    </p>
                    <p v-if="!claim.sources.length" class="card-muted">
                      该 claim 没有任何来源绑定记录。
                    </p>
                    <p v-if="claim.sources.some((s: { binding_status: string }) => s.binding_status !== 'bound' && s.binding_status !== 'not_required')"
                       class="text-xs" style="color: var(--acc-red)">
                      存在未成功绑定的来源：Evidence gate 必须失败。
                    </p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <p class="card-muted mt-3">
        Binder 已验证：
        <span v-for="x in view.evidence_semantics.verified_by_binder" :key="x" class="chip mr-1">{{ x }}</span>
      </p>
      <p class="card-muted mt-1">
        Binder 未验证：
        <span v-for="x in view.evidence_semantics.not_verified_by_binder" :key="x" class="chip mr-1">{{ x }}</span>
      </p>
      <p class="card-muted mt-1">
        出版状态：{{ view.publication.included_in_document ? '已进入文档' : '未进入文档' }} ·
        {{ view.publication.reason }}
      </p>
    </template>
    <p v-else class="card-muted">
      该运行暂无 Evidence V2（Phase 4 绑定未执行，或 V6_LEARNING_ENGINE=off）。
    </p>
  </section>
</template>
