/**
 * 业务层 endpoint 类型与薄封装。
 */
import { api } from './client'

export interface Course {
  id: number
  name: string
  code?: string | null
  semester?: string | null
  teacher?: string | null
  schedule_json?: string | null
  created_at?: string | null
}

export interface CourseDeleteImpact {
  course: Pick<Course, 'id' | 'name' | 'code'>
  counts: Record<string, number>
}

export interface Chapter {
  id: number
  course_id: number
  chapter_no: number | null
  title: string
  syllabus_ref?: string | null
  status: string
  review_status?: string | null
  completed_at?: string | null
  reviewed_at?: string | null
  notes?: string | null
}

export interface Lesson {
  id: number
  chapter_id?: number | null
  course_id: number
  lesson_no?: string | null
  title?: string | null
  date?: string | null
  status?: string | null
  note_id?: number | null
  summary?: string | null
}

export interface ExamEvent {
  id: number
  course_id: number | null
  course_name?: string | null
  course_code?: string | null
  event_type: string
  title: string
  date: string | null
  detail?: string | null
}

export interface CalendarEvent {
  id: number
  course_id: number | null
  event_type: string
  title: string
  date: string | null
  detail?: string | null
}

export interface AcademicTerm {
  id: number
  name: string
  school_year?: string | null
  semester?: string | null
  start_date: string
  end_date: string
  first_week_monday: string
  teaching_weeks: number
  exam_start?: string | null
  exam_end?: string | null
  source?: string | null
  active: number | boolean
}

export interface ScheduleRule {
  id: number
  course_id: number
  course_name?: string | null
  course_code?: string | null
  weekday: number
  start_week: number
  end_week: number
  week_parity: 'all' | 'odd' | 'even'
  periods: number[]
  start_time?: string | null
  end_time?: string | null
  location?: string | null
  teacher?: string | null
  note?: string | null
  source?: string | null
  enabled: number | boolean
}

export type AdjustmentType = 'holiday' | 'day_off' | 'workday'

export interface CalendarAdjustment {
  id: number
  adjustment_type: AdjustmentType
  date: string
  source_date?: string | null
  periods: number[]
  title?: string | null
  detail?: string | null
  source?: string | null
}

export interface ScheduleOccurrence {
  id: string
  rule_id?: number
  adjustment_id?: number
  course_id?: number | null
  course_name?: string | null
  course_code?: string | null
  date: string
  weekday: number
  week_number?: number | null
  periods: number[]
  start_time?: string | null
  end_time?: string | null
  location?: string | null
  teacher?: string | null
  note?: string | null
  week_parity?: string | null
  status: string
  source_date?: string | null
}

export interface EffectiveScheduleDay {
  date: string
  weekday: number
  week_number?: number | null
  is_today: boolean
  is_day_off: boolean
  effective_weekday: number
  events: Array<Pick<CalendarAdjustment, 'id' | 'adjustment_type' | 'title' | 'detail' | 'source_date'>>
  courses: ScheduleOccurrence[]
}

export interface EffectiveSchedule {
  start: string
  end: string
  term?: AcademicTerm | null
  days: EffectiveScheduleDay[]
}

export interface WorkflowRun {
  id: number
  workflow: string
  mode?: string | null
  course_id?: number | null
  lesson_id?: number | null
  chapter_id?: number | null
  status: string
  input_json?: string | null
  output_json?: string | null
  error?: string | null
  created_at?: string | null
  // V6 Phase 1: 运行列表附带的引擎与覆盖门禁摘要（V5 运行为空）
  engine_version?: string | null
  coverage_gate?: string | null
  silent_dropped?: number | null
  domain_id?: number | null
}

export interface DocumentArtifact {
  id: number
  note_id?: number | null
  review_id?: number | null
  document_type: 'lesson_note' | 'review_handout'
  template_id: string
  status: string
  has_html: boolean
  has_pdf: boolean
  error?: string | null
}

export interface RunNode {
  id: number
  run_id: number
  node_name: string
  status: string
  agent_role?: string | null
  model?: string | null
  input_ref?: string | null
  output_ref?: string | null
  started_at?: string | null
  finished_at?: string | null
  tokens_in?: number | null
  tokens_out?: number | null
  error?: string | null
}

export interface ErrorItem {
  id: number
  course_id?: number | null
  chapter_id?: number | null
  lesson_id?: number | null
  question_text?: string | null
  image_file?: string | null
  student_answer?: string | null
  correct_answer?: string | null
  user_explanation?: string | null
  ai_error_json?: string | null
  final_error_json?: string | null
  status: string
  next_review_at?: string | null
  review_stage?: number | null
}

export interface ModelItem {
  id: string
  gateway_model: string
  provider: string
  family: string
  capability: string
  enabled: boolean
  is_builtin: boolean
  context_window?: number | null
  embedding_dimensions?: number | null
  input_cost?: number | null
  output_cost?: number | null
  cost_unit: string
  notes: string
  is_default_solver: boolean
  is_default_reviewer: boolean
  is_default_vision: boolean
  is_default_light: boolean
}

export interface GatewayServiceModel {
  id: string
  providerId: string
  providerName: string
  kind: 'embedding' | 'rerank' | 'ocr'
  clientModelId: string
  upstreamModelId: string
  upstreamPath: string
  dimensions?: number | null
  maxInputTokens?: number | null
  enabled: boolean
  createdAt: string
  updatedAt: string
}

export interface GatewayServicesStatus {
  available: boolean
  models: GatewayServiceModel[]
  detail: string
  admin_url: string
}

export interface Material {
  id: number
  lesson_id?: number | null
  chapter_id?: number | null
  course_id?: number | null
  file_path?: string | null
  file_hash?: string | null
  type?: string | null
  kind?: string | null
  name?: string | null
  display_name?: string | null
  mime?: string | null
  sha256?: string | null
  size_bytes?: number
  parser_status?: string | null
  status?: string | null
  parse_error?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface GraphNode {
  id: number
  course_id?: number | null
  title: string
  node_type: string
  meta_json?: string | null
}

export interface GraphEdge {
  id: number
  source_id: number
  target_id: number
  relation: string
}

export interface SyncJob {
  id: number
  target?: string | null
  asset_id?: number | null
  asset_path?: string | null
  content_hash?: string | null
  status: string
  retries?: number | null
  last_error?: string | null
  synced_at?: string | null
}

export interface RoutesInfo {
  quota_pct_5h: number
  routes: Record<string, string>
}

// --------- API 业务封装 ---------

export const CoursesApi = {
  list: (_headers?: Record<string, string>) => api.get<Course[]>('/courses', undefined, _headers),
  create: (body: Partial<Course>) => api.post('/courses', body),
  deleteImpact: (id: number) => api.get<CourseDeleteImpact>(`/courses/${id}/delete-impact`),
  delete: (id: number, confirmName: string) =>
    api.del<{ id: number; name: string; status: string; counts: Record<string, number> }>(
      `/courses/${id}?confirm_name=${encodeURIComponent(confirmName)}`,
    ),
}

export const ChaptersApi = {
  list: (params?: { course_id?: number }, headers?: Record<string, string>) => api.get<Chapter[]>('/chapters', params, headers),
  create: (body: Partial<Chapter>) => api.post('/chapters', body),
  update: (id: number, body: Partial<Chapter>) => api.put<Chapter>(`/chapters/${id}`, body),
  delete: (id: number, confirmTitle: string) =>
    api.del<{ id: number; title: string; status: string }>(`/chapters/${id}?confirm_title=${encodeURIComponent(confirmTitle)}`),
  advance: (id: number) => api.post<{ id: number; status: string }>(`/chapters/${id}/advance`),
  setStatus: (id: number, status: string) => api.post<{ id: number; status: string }>(`/chapters/${id}/set_status`, { status }),
  markReview: (id: number) => api.post<{ id: number; review_status: string }>(`/chapters/${id}/review`),
  state: (id: number) => api.get<{ chapter: Chapter; current: string; next_states: string[]; all_states: string[] }>(`/chapters/${id}/state`),
}

export const LessonsApi = {
  list: (params?: { course_id?: number; chapter_id?: number }, headers?: Record<string, string>) => api.get<Lesson[]>('/lessons', params, headers),
  create: (body: Partial<Lesson>) => api.post('/lessons', body),
}

export const ExamsApi = {
  list: () => api.get<ExamEvent[]>('/exams'),
  listCalendar: (params?: { course_id?: number; event_type?: string }) =>
    api.get<CalendarEvent[]>('/calendar', params),
  addCalendar: (body: Partial<CalendarEvent>) => api.post('/calendar', body),
  updateCalendar: (id: number, body: Partial<CalendarEvent>) => api.put(`/calendar/${id}`, body),
  deleteCalendar: (id: number) => api.del(`/calendar/${id}`),
}

export const ScheduleApi = {
  terms: () => api.get<AcademicTerm[]>('/academic-terms'),
  createTerm: (body: Partial<AcademicTerm>) => api.post<AcademicTerm>('/academic-terms', body),
  updateTerm: (id: number, body: Partial<AcademicTerm>) => api.put<AcademicTerm>(`/academic-terms/${id}`, body),
  deleteTerm: (id: number) => api.del(`/academic-terms/${id}`),
  rules: (params?: { course_id?: number }) => api.get<ScheduleRule[]>('/schedule-rules', params),
  createRule: (body: Partial<ScheduleRule>) => api.post<ScheduleRule>('/schedule-rules', body),
  updateRule: (id: number, body: Partial<ScheduleRule>) => api.put<ScheduleRule>(`/schedule-rules/${id}`, body),
  deleteRule: (id: number) => api.del(`/schedule-rules/${id}`),
  adjustments: (params?: { start?: string; end?: string }) => api.get<CalendarAdjustment[]>('/calendar-adjustments', params),
  createAdjustment: (body: Partial<CalendarAdjustment>) => api.post<CalendarAdjustment>('/calendar-adjustments', body),
  updateAdjustment: (id: number, body: Partial<CalendarAdjustment>) => api.put<CalendarAdjustment>(`/calendar-adjustments/${id}`, body),
  deleteAdjustment: (id: number) => api.del(`/calendar-adjustments/${id}`),
  effective: (params?: { start?: string; days?: number }) => api.get<EffectiveSchedule>('/schedule/effective', params),
  bootstrap: () => api.post<{ term_created: number; rules_created: number; adjustments_created: number }>('/schedule/bootstrap'),
}

export const MaterialsApi = {
  upload: (form: FormData, headers?: Record<string, string>) => api.postForm<{ id: number; status: string; kind: string; name?: string; deduped?: boolean }>(
    '/materials/upload',
    form,
    headers,
  ),
  list: (params?: { status?: string; course_id?: number }) => api.get<Material[]>('/materials', params),
  get: (id: number) => api.get<Material>(`/materials/${id}`),
  patch: (id: number, body: Record<string, unknown>) => api.patch<{ id: number; status: string }>(`/materials/${id}`, body),
  remove: (id: number) => api.del<{ id: number; status: string }>(`/materials/${id}`),
  retry: (id: number) => api.post<{ id: number; status: string }>(`/materials/${id}/retry`),
  chunks: (id: number) => api.get<any[]>(`/materials/${id}/chunks`),
}

export const ErrorsApi = {
  list: (params?: { status?: string; chapter_id?: number }) => api.get<ErrorItem[]>('/errors', params),
  confirm: (id: number) => api.post<{ status: string }>(`/errors/${id}/confirm`),
  reject: (id: number) => api.post<{ status: string }>(`/errors/${id}/reject`),
}

export const ReviewsApi = {
  list: () => api.get<any[]>('/reviews'),
}

// --------- V6 Learning Engine Phase 1（Material Domain / Coverage / Source Span） ---------

export interface V6EngineInfo {
  mode: 'off' | 'shadow' | 'on'
  engine_version: string
  enabled: boolean
}

export interface MaterialDomainItem {
  domain_item_id: number
  material_id: number | null
  source_kind: string
  ordinal: number
  required: boolean
  raw_chars: number | null
  raw_tokens: number | null
  content_hash: string | null
  state: string
  reason_code: string | null
  reason_detail: string | null
  duplicate_of_item_id: number | null
  // 真实存在的 Source ID（服务端产出，前端不拼接）
  span_count: number
  source_ids: string[]
}

export interface MaterialDomainView {
  engine: V6EngineInfo
  domain: {
    domain_id: number
    run_id: number
    scope: string
    course_id: number | null
    chapter_id: number | null
    lesson_id: number | null
    version: number
    schema_version: string
    domain_hash: string
    state: string
    engine_version: string
    transcript_chars: number
    frozen_at: string | null
    created_at: string | null
  }
  counts: {
    total_items: number
    unique_items: number
    duplicate_items: number
    exempt_items: number
    included_items: number
  }
  items: MaterialDomainItem[]
}

export interface CoverageMetrics {
  total_source_spans?: number
  canonical_non_noise_spans?: number
  duplicate_spans?: number
  noise_spans?: number
  unsupported_spans?: number
  failed_spans?: number
  processed_spans?: number
  silent_dropped?: number
  domain_accounting_rate?: number
  semantic_processing_rate?: number
  timeline_coverage_rate?: number
  timeline_span_total?: number
  timeline_span_processed?: number
  timeline_start_ms?: number | null
  timeline_end_ms?: number | null
  timeline_gap_count?: number
  ppt_pages_total?: number
  ppt_pages_processed?: number
  source_refs_total?: number
  source_refs_valid?: number
  source_refs_invalid?: number
  // Phase 2
  required_items_total?: number
  required_items_failed?: number
  required_items_unsupported?: number
  required_items_without_canonical_span?: number
  required_spans_unprocessed?: number
  segment_count?: number
  segment_failed_count?: number
  segments_consumed_by_merge?: number
  merge_consumed_all_segments?: boolean
  legacy_candidate_rate?: number
  semantic_rate_floor?: number
  [key: string]: unknown
}

export interface RunSegment {
  segment_id: number
  ordinal: number
  status: string
  strategy: string
  title?: string | null
  input_hash: string
  token_count: number
  primary_source_ids: string[]
  overlap_source_ids: string[]
  primary_span_count: number
  overlap_span_count: number
  start_ms?: number | null
  end_ms?: number | null
  attempts: number
  error?: string | null
  model_used?: string | null
  duration_ms?: number | null
  source_ref_count?: number
  understanding_status?: string
}

export interface SegmentsView {
  engine: V6EngineInfo
  run_id: number
  domain_id: number
  segment_count: number
  strategy: string | null
  segments: RunSegment[]
}

export interface SegmentUnderstandingView {
  segment_id: number
  segment_ordinal: number
  status: string
  model_used?: string | null
  prompt_version?: string | null
  schema_version?: string | null
  input_hash?: string | null
  content_hash?: string | null
  source_ref_count?: number | null
  attempts?: number | null
  error?: string | null
  understanding: Record<string, any>
}

export interface LessonUnderstandingView {
  status: string
  model_used?: string | null
  prompt_version?: string | null
  schema_version?: string | null
  input_hash?: string | null
  content_hash?: string | null
  consumed_segment_count: number
  segment_count: number
  valid_source_count: number
  merge_levels: number
  error?: string | null
  understanding: Record<string, any>
}

export interface UnderstandingView {
  engine: V6EngineInfo
  run_id: number
  domain_id: number
  lesson_understanding: LessonUnderstandingView | null
  segment_understandings: SegmentUnderstandingView[]
}


export interface CoverageView {
  engine: V6EngineInfo
  run: { run_id: number; workflow: string | null; status: string; degraded: boolean }
  report: {
    domain_id: number
    engine_version: string
    engine_mode: string
    gate: 'passed' | 'degraded' | 'failed'
    silent_dropped: number
    degradation_reason: string | null
    metrics: CoverageMetrics
    created_at: string | null
    updated_at: string | null
  }
  plan: {
    strategy: string
    model_profile_id: string | null
    context_window: number
    input_budget_tokens: number
    output_budget_tokens: number
    planned_span_count: number
    unassigned_count: number
  } | null
  ledger: Array<{ stage: string; outcome: string; reason_code: string | null; n: number }>
  unprocessed_reasons: Array<{ reason_code: string | null; stage: string; n: number }>
}

export interface SourceSpanView {
  engine: V6EngineInfo
  source_id: string
  domain_id: number
  run_id: number
  material_id: number | null
  domain_item_id: number | null
  source_chunk_id: number | null
  source_kind: string
  locator: string | null
  ordinal: number
  start_ms: number | null
  end_ms: number | null
  page_no: number | null
  slide_no: number | null
  text: string
  normalized_text_hash: string
  token_count: number | null
  char_count: number | null
  span_state: string
  reason_code: string | null
  reason_detail: string | null
  canonical_source_id: string | null
}

export interface CognitiveItemView {
  item_id: number
  stable_key: string
  item_type: string
  title: string
  explanation: string
  severity: number
  confidence: number
  recommended_treatment: string
  origin: string
  status: string
  source_refs: string[]
  knowledge_unit_refs: string[]
}

export interface CognitiveMapView {
  engine: V6EngineInfo
  run_id: number
  domain_id: number
  cognitive_map: {
    status: string
    model_used?: string | null
    prompt_version?: string | null
    schema_version?: string | null
    input_hash?: string | null
    content_hash?: string | null
    knowledge_unit_total: number
    knowledge_unit_processed: number
    cognitive_input_coverage: number
    batch_count: number
    error?: string | null
    availability: Record<string, string>
    note?: string | null
  }
  input_coverage: { total: number; consumed: number; skipped: number; failed: number; coverage: number }
  by_type: Record<string, number>
  items: CognitiveItemView[]
  publication: { included_in_document: boolean; reason: string }
}

export interface EvidenceV2Source {
  source_id: string
  source_span_id: number | null
  relation: string
  binding_status: string
  binding_method: string
  bound_quote: string
  quote_hash: string
  source_text_hash: string
  locator: string | null
  start_ms: number | null
  end_ms: number | null
  page_no: number | null
  slide_no: number | null
  source_kind: string | null
  presented_to_producer: boolean
  /** Phase 4.1: visible | not_visible | unknown（相对本 claim 的 producer 调用） */
  visibility: string
  /** 真正发过该来源的 segment / cognitive batch ordinal */
  visible_invocations: number[]
  failure_reason: string | null
}

export interface EvidenceV2Provenance {
  producer_stage: string
  producer_node: string
  producer_kind: string
  invocation_refs: number[]
  visible_source_ids: string[]
  visibility_basis: string
  provenance_version: string
  per_source_invocations: Record<string, number[]>
  source_scope: string
  available: boolean
}

export interface EvidenceV2Claim {
  claim_id: number
  claim_key: string
  claim_type: string
  claim_text: string
  importance: string
  producer_node: string
  evidence_status: string
  requires_source: boolean
  is_ai_explanation: boolean
  input_hash: string
  content_hash: string
  provenance: EvidenceV2Provenance
  sources: EvidenceV2Source[]
}

export interface EvidenceV2Report {
  run_id: number
  domain_id: number
  engine_version: string
  engine_mode: string
  claims_total: number
  required_claims: number
  bound_claims: number
  failed_claims: number
  not_required_claims: number
  critical_claims: number
  critical_claims_bound: number
  critical_claim_evidence_rate: number
  critical_evidence_denominator: number
  invalid_source_refs: number
  cross_domain_refs: number
  unbound_source_refs: number
  unread_source_refs: number
  /** Phase 4.1：来源未进入产出该 claim 的模型调用 */
  not_in_claim_producer_refs: number
  /** Phase 4.1：缺少 producer provenance 的 claim 数 */
  claims_without_provenance: number
  claims_by_type: Record<string, number>
  claims_by_producer: Record<string, number>
  gate: string
  degradation_reason: string | null
  issues: string[]
  legacy_projection_rows: number
}

export interface EvidenceV2View {
  engine: V6EngineInfo
  run_id: number
  domain_id: number
  report: EvidenceV2Report
  by_type: Record<string, EvidenceV2Claim[]>
  claims: EvidenceV2Claim[]
  ai_explanation_label: string
  evidence_semantics: {
    verified_by_binder: string[]
    not_verified_by_binder: string[]
    visibility_scope?: string
    visibility_note?: string
  }
  publication: { included_in_document: boolean; reason: string; real_notes_publish_enabled: boolean }
}

export interface QualityStateView {
  run_id: number
  quality: {
    processing_status: string
    coverage_status: string
    evidence_status: string
    review_status: string
    publication_status: string
    sync_status: string
    degradation_reason?: string | null
    updated_at?: string
  }
  revisions: Array<{
    id: number
    note_id: number
    revision: number
    schema_version: string
    content_hash: string
    evidence_status: string
    coverage_status: string
    review_status: string
    created_at: string
  }>
}

export interface MasteryItem {
  knowledge_unit_id: number
  stable_key: string
  topic: string
  course_id?: number | null
  chapter_id?: number | null
  lesson_id?: number | null
  mastery: number
  confidence: number
  signal_count: number
  updated_from: string
  explanation: string
  updated_at: string
}

export const LessonWorkflowApi = {
  run: (body: any) => api.post<{ run_id: number; outputs: Record<string, any> }>('/workflows/lesson', body),
  get: (runId: number, includePayloads = false) => api.get<{
    run: WorkflowRun
    nodes: RunNode[]
    engine?: V6EngineInfo
    engine_version?: string
    domain_id?: number | null
  }>(`/workflows/lesson/${runId}`, { include_payloads: includePayloads }),
  materialDomain: (runId: number) => api.get<MaterialDomainView>(`/runs/${runId}/material-domain`),
  coverage: (runId: number) => api.get<CoverageView>(`/runs/${runId}/coverage`),
  segments: (runId: number) => api.get<SegmentsView>(`/runs/${runId}/segments`),
  understanding: (runId: number) => api.get<UnderstandingView>(`/runs/${runId}/understanding`),
  cognitiveMap: (runId: number) => api.get<CognitiveMapView>(`/runs/${runId}/cognitive-map`),
  evidenceV2: (runId: number) => api.get<EvidenceV2View>(`/runs/${runId}/evidence-v2`),
  quality: (runId: number) => api.get<QualityStateView>(`/runs/${runId}/quality`),
  sourceSpan: (sourceId: string, runId: number) =>
    api.get<SourceSpanView>(`/source-spans/${encodeURIComponent(sourceId)}`, { run_id: runId }),
}

export interface CapabilityLimits {
  max_upload_bytes: number
  max_inline_transcript_chars: number
  transcript_warning_chars: number
  allowed_extensions: string[]
  large_input_strategy: string
}

export const CapabilitiesApi = {
  limits: () => api.get<CapabilityLimits>('/capabilities/limits'),
}

export const LearningApi = {
  mastery: (params: { course_id?: number; chapter_id?: number } = {}) =>
    api.get<{ items: MasteryItem[] }>('/learning/mastery', params),
  errorTrace: (errorId: number) => api.get<any>(`/errors/${errorId}/learning-trace`),
}

export const HomeworkWorkflowApi = {
  run: (body: any) => api.post<{ run_id: number; outputs: Record<string, any> }>('/workflows/homework', body),
  get: (runId: number) => api.get<{ run: WorkflowRun; nodes: RunNode[] }>(`/workflows/homework/${runId}`),
}

export const ErrorWorkflowApi = {
  run: (body: any) => api.post<{ run_id: number; outputs: Record<string, any> }>('/workflows/error', body),
}

export const ReviewWorkflowApi = {
  run: (body: any) => api.post<{ run_id: number; outputs: Record<string, any> }>('/workflows/review', body),
}

export const RunsApi = {
  list: (limit = 100) => api.get<WorkflowRun[]>('/runs', { limit }),
  get: (id: number) => api.get<{ run: WorkflowRun; nodes: RunNode[] }>(`/runs/${id}`),
  cancel: (id: number) => api.post<{ run_id: number; status: string }>(`/runs/${id}/cancel`),
  retry: (id: number, body?: { from_node?: string; reuse_successful_dependencies?: boolean }) =>
    api.post<{ run_id: number; parent_run_id: number; status: string }>(`/runs/${id}/retry`, body ?? {}),
  result: (id: number) => api.get<RunResultDto>(`/runs/${id}/result`),
}

export const ArtifactsApi = {
  list: (owner_type?: 'note' | 'review', owner_id?: number) => api.get<DocumentArtifact[]>('/artifacts', { owner_type, owner_id }),
  regenerate: (id: number) => api.post<DocumentArtifact>(`/artifacts/${id}/regenerate`),
  previewUrl: (id: number) => `/api/artifacts/${id}/content`,
  downloadUrl: (id: number, format: 'html' | 'pdf') => `/api/artifacts/${id}/download?format=${format}`,
}

// --------- 运行结果业务 DTO（方案 13.4：前端按 workflow 类型化消费） ---------

export interface RunResultBase {
  run_id: number
  workflow: string
  status: string
}

export interface LessonResultDto extends RunResultBase {
  workflow: 'lesson'
  note?: { id: number; title: string; body: string; status: string; markdown_path?: string | null } | null
  evidence?: Array<{ chunk_id: number | null; quote: string; verified: boolean; locator?: string }>
  chunks?: Array<{ chunk_id: number; text: string; locator?: string; source?: string }>
  outline?: Array<{ topic?: string }> | string[]
  tokens?: Record<string, number>
  [key: string]: unknown
}

export interface HomeworkResultDto extends RunResultBase {
  workflow: 'homework'
  questions?: Array<{
    id: number
    question_no: number | string
    text: string
    student_answer?: string | null
    submitted_at?: string | null
    reveal_allowed?: number | boolean
    answer?: {
      final_answer: string
      solution_plan?: string
      detailed_solution?: string
      teaching?: string
      conflict?: string | null
    }
  }>
  solutions?: Array<Record<string, unknown>>
  conflicts?: Array<Record<string, unknown>>
  [key: string]: unknown
}

export interface ReviewResultDto extends RunResultBase {
  workflow: 'review'
  questions?: Array<{ question_no?: string; q: string; answer?: string; answered?: boolean; student_answer?: string | null; correct?: boolean | null }>
  package?: { outline?: unknown[]; materials?: string } | null
  score?: number | null
  [key: string]: unknown
}

export interface ErrorResultDto extends RunResultBase {
  workflow: 'error'
  provisional?: Array<{ id: number; question_text: string; causes?: string[] }>
  [key: string]: unknown
}

export type RunResultDto = LessonResultDto | HomeworkResultDto | ReviewResultDto | ErrorResultDto

export const ModelsApi = {
  list: () => api.get<ModelItem[]>('/models'),
  gatewayServices: () => api.get<GatewayServicesStatus>('/models/gateway-services'),
  save: (model: ModelItem) => api.put<ModelItem>(`/models/${encodeURIComponent(model.id)}`, model),
  setEnabled: (id: string, enabled: boolean) => api.patch<{ id: string; enabled: boolean }>(`/models/${encodeURIComponent(id)}/enabled`, { enabled }),
  routes: () => api.get<Record<string, string[]>>('/model-routes'),
  saveRoute: (role: string, model_ids: string[]) => api.put<{ role: string; model_ids: string[] }>(`/model-routes/${encodeURIComponent(role)}`, { model_ids }),
}

export const UsageApi = {
  get: () => api.get<any>('/usage'),
}

export const RoutesApi = {
  get: (workflow: string) => api.get<RoutesInfo>(`/routes/${workflow}`),
}

export const GraphApi = {
  get: () => api.get<{ nodes: GraphNode[]; edges: GraphEdge[] }>('/graph'),
}

export const SyncApi = {
  status: () => api.get<SyncJob[]>('/sync'),
  vault: () => api.get<{ vault_root: string }>('/sync/vault'),
}

export const HealthApi = {
  get: () => api.get<{ status: string; version: string }>('/health'),
}

// --------- 移动端 Token 管理(仅本机访问) ---------

export interface TokenShareUrls {
  recommended: string | null
  tailscale: string | null
  funnel: string | null
  lan: string | null
}

export interface MobileTokenStatus {
  enabled: boolean
  token_hint: string | null
  share_base_urls: TokenShareUrls
}

export interface MobileTokenMutationResult {
  enabled: boolean
  token: string | null
  share_urls: TokenShareUrls
}

export const MobileTokenAdminApi = {
  status: () => api.get<MobileTokenStatus>('/admin/mobile-token/status'),
  rotate: (body?: { length?: number }) =>
    api.post<MobileTokenMutationResult>('/admin/mobile-token/rotate', body ?? {}),
  set: (body: { token: string }) =>
    api.post<MobileTokenMutationResult>('/admin/mobile-token/set', body),
  reset: () =>
    api.post<MobileTokenMutationResult>('/admin/mobile-token/reset', {}),
}
