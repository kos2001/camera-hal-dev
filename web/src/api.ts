// 백엔드(/api) 타입과 호출 함수. 스키마는 backend/serialize.py 와 1:1.

export type Severity = 'Blocker' | 'Critical' | 'Major' | 'Minor'

export interface Platform {
  code: string
  family: string
  fw_prefix: string
  isp: string
  desc: string
  sensor_modes: string[]
  max_blob: string
  isp_ports: number
  pool_size: number
  partial_results: number
  camera_ids: string[]
  host_platforms: string[]
  logical_multicam: boolean
  external_camera: boolean
  channels: number
}

export interface Workload {
  name: string
  title: string
  steps: string[]
  budgets: Record<string, number>
}

export interface Fault {
  id: string
  defect: string
  workload: string
  platform: string
  expect: string[]
  category: string
  family: string
  severity: Severity
  title: string
  summary: string
  symptom: string
  root_cause: string
  resolution: string
  workaround: string
  repro: string[]
  log_excerpt: string
}

export interface CheckerInfo {
  code: string
  category: string
  severity: Severity
  title: string
}

export interface Catalog {
  version: string
  categories: string[]
  platforms: Platform[]
  workloads: Workload[]
  faults: Fault[]
  checkers: CheckerInfo[]
}

export interface Violation {
  code: string
  category: string
  severity: Severity
  title: string
  detail: string
  count: number
  evidence: Record<string, unknown>[]
}

export interface Lane {
  kind: string
  count: number
  bad: boolean
  points: number[]
}

export interface FramePoint {
  t_ms: number
  frame: number
  luma: number
  exposure_ms: number
  frame_duration_ms: number
  gain: number
  interval_ms: number | null
  ts_delta_ms: number | null
  wb_error_k: number
}

export interface LogLine {
  t_ms: number
  level: string
  tag: string
  msg: string
}

export interface Verdict {
  expected: string[]
  injected: boolean
  detected: boolean
  missing: string[]
  clean: boolean
}

export interface RunPayload {
  id: string
  platform: Platform
  workload: Workload
  fault: Fault | null
  defects: string[]
  seed: number
  sim_ms: number
  wall_ms: number
  counters: Record<string, number>
  verdict: Verdict | null
  violations: Violation[]
  timeline: { t_end_ms: number; lanes: Lane[] }
  series: {
    frames: { key: string; count: number; points: FramePoint[] }[]
    memory: { t_ms: number; rss_mb: number; pipes: number }[]
  }
  logs: LogLine[]
  logs_total: number
}

export interface SweepRow {
  id: string
  category: string
  platform: string
  workload: string
  defect: string
  expect: string[]
  faulty_codes: string[]
  fixed_codes: string[]
  detected: boolean
  clean: boolean
  title: string
  severity: Severity
  sim_ms: number
}

export interface SweepResult {
  seed: number
  total: number
  ok: number
  rows: SweepRow[]
}

export interface EventPage {
  total: number
  offset: number
  items: Record<string, unknown>[]
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* 본문 없음 */
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

const post = <T,>(path: string, body: unknown) => call<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  catalog: () => call<Catalog>('/catalog'),
  run: (body: { platform: string; workload: string; defects: string[]; seed: number }) =>
    post<RunPayload>('/run', body),
  runFault: (id: string, body: { platform?: string; seed: number; fixed: boolean }) =>
    post<RunPayload>(`/faults/${id}/run`, body),
  compare: (id: string, body: { platform?: string; seed: number }) =>
    post<{ faulty: RunPayload; fixed: RunPayload }>(`/faults/${id}/compare`, body),
  sweep: (seed: number) => post<SweepResult>('/sweep', { seed }),
  events: (rid: string, kind: string | null, offset: number, limit: number) => {
    const q = new URLSearchParams({ offset: String(offset), limit: String(limit) })
    if (kind) q.set('kind', kind)
    return call<EventPage>(`/runs/${rid}/events?${q}`)
  },
  issue: (rid: string) => call<Record<string, unknown>>(`/runs/${rid}/issue`),
  reportUrl: (rid: string) => `/api/runs/${rid}/report.html`,
}
