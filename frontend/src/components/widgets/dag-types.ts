/**
 * DAG 节点共享类型。
 */
export interface DagNode {
  name: string
  status: string
  model?: string | null
  model_display?: string | null
  agent_role?: string | null
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  output_ref?: string | null
  input_ref?: string | null
  tokens_in?: number | null
  tokens_out?: number | null
}

export function toDagNodes<T extends { node_name: string; status: string }>(
  raw: Array<T & Partial<Omit<DagNode, 'name' | 'status'>>>,
): DagNode[] {
  return raw.map(n => ({
    name: n.node_name,
    status: n.status,
    model: (n as any).model ?? null,
    model_display: (n as any).model_display ?? null,
    agent_role: (n as any).agent_role ?? null,
    started_at: (n as any).started_at ?? null,
    finished_at: (n as any).finished_at ?? null,
    error: (n as any).error ?? null,
    output_ref: (n as any).output_ref ?? null,
    input_ref: (n as any).input_ref ?? null,
    tokens_in: (n as any).tokens_in ?? 0,
    tokens_out: (n as any).tokens_out ?? 0,
  }))
}
