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
  is_default_solver: boolean
  is_default_reviewer: boolean
  is_default_vision: boolean
  is_default_light: boolean
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
}

export const ChaptersApi = {
  list: (params?: { course_id?: number }, headers?: Record<string, string>) => api.get<Chapter[]>('/chapters', params, headers),
  create: (body: Partial<Chapter>) => api.post('/chapters', body),
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
  seed: (force = false) => api.post(`/calendar/seed?force=${force}`),
}

export const SyllabusApi = {
  import: (body: {
    course_id?: number
    course_code?: string
    course_name?: string
    chapters: Array<{
      chapter_no?: number
      title?: string
      lessons?: Array<{ lesson_no?: string; title?: string; date?: string } | string>
    }>
  }) => api.post<{ status: string; course_id: number; stats: { chapters: number; lessons: number } }>(
    '/syllabus/import',
    body,
  ),
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

export const LessonWorkflowApi = {
  run: (body: any) => api.post<{ run_id: number; outputs: Record<string, any> }>('/workflows/lesson', body),
  get: (runId: number) => api.get<{ run: WorkflowRun; nodes: RunNode[] }>(`/workflows/lesson/${runId}`),
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
}

export const ModelsApi = {
  list: () => api.get<ModelItem[]>('/models'),
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
