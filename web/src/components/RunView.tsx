import { useEffect, useMemo, useState } from 'react'
import { api, type EventPage, type RunPayload, type Violation } from '../api'
import { LineChart, Timeline, type Series } from './charts'
import { CategoryDot, Empty, ErrorBox, Pill, Section, SeverityBadge } from './ui'

const PALETTE = ['#0284c7', '#db2777', '#16a34a', '#ea580c', '#7c3aed', '#0d9488']

// --------------------------------------------------------------------------- //
export function RunView({ run }: { run: RunPayload }) {
  return (
    <div className="flex flex-col gap-4">
      <RunHeader run={run} />
      <Violations violations={run.violations} expected={run.verdict?.expected ?? []} />
      <Section title="사건 타임라인" right={<span className="text-xs text-stone-500">{Object.values(run.counters).reduce((a, b) => a + b, 0).toLocaleString()} events · {(run.sim_ms / 1000).toFixed(2)}s</span>}>
        <div className="overflow-x-auto">
          <Timeline lanes={run.timeline.lanes} tEnd={run.timeline.t_end_ms} />
        </div>
      </Section>
      <Charts run={run} />
      <LogViewer run={run} />
      <EventBrowser run={run} />
    </div>
  )
}

// --------------------------------------------------------------------------- //
function RunHeader({ run }: { run: RunPayload }) {
  const [copied, setCopied] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)
  const v = run.verdict

  async function exportIssue(mode: 'download' | 'copy') {
    setExportError(null)
    try {
      const issue = await api.issue(run.id)
      const text = JSON.stringify(issue, null, 1)
      if (mode === 'copy') {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
        return
      }
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
      const a = Object.assign(document.createElement('a'), { href: url, download: `${issue.key}.json` })
      a.click()
      URL.revokeObjectURL(url)
    } catch (failure) {
      setExportError(String((failure as Error).message))
    }
  }

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {v && (v.injected ? (v.detected ? <Pill tone="ok">기대 위반 모두 검출</Pill> : <Pill tone="bad">미검출: {v.missing.join(', ')}</Pill>) : v.clean ? <Pill tone="ok">수정 상태 · 위반 0건</Pill> : <Pill tone="bad">수정 상태인데 위반 {run.violations.length}건</Pill>)}
            {!v && (run.violations.length ? <Pill tone="bad">위반 {run.violations.length}종</Pill> : <Pill tone="ok">위반 없음</Pill>)}
            {run.defects.length ? run.defects.map((d) => <Pill key={d} tone="info">⚡ {d}</Pill>) : <Pill>결함 미주입</Pill>}
          </div>
          <h2 className="mt-2 text-base font-semibold leading-snug">{run.fault ? run.fault.title.replace('{chip}', run.platform.code).replace(run.fault.platform, run.platform.code) : run.workload.title}</h2>
          <p className="mt-1 text-xs text-stone-500">
            {run.platform.code} · <span className="font-mono">{run.workload.name}</span> · seed {run.seed} · sim {(run.sim_ms / 1000).toFixed(2)}s · wall {run.wall_ms.toFixed(0)}ms · results {run.counters.result ?? 0} · drops {run.counters.frame_drop ?? 0}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <a className="btn-ghost" href={`#/history/${run.id}`}>실행 기록 링크</a>
          <a className="btn-ghost" href={api.reportUrl(run.id)} target="_blank" rel="noreferrer">HTML 리포트 ↗</a>
          <button className="btn-ghost" onClick={() => exportIssue('copy')}>{copied ? '복사됨' : 'Jira 이슈 복사'}</button>
          <button className="btn-ghost" onClick={() => exportIssue('download')}>이슈 JSON ↓</button>
        </div>
      </div>
      {exportError && <div className="mt-3"><ErrorBox error={exportError} /></div>}
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer text-xs font-medium text-sky-700 dark:text-sky-400">재현 절차 {run.workload.steps.length}단계{Object.keys(run.workload.budgets).length ? ' · 요구 기준' : ''}</summary>
        <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-stone-600 dark:text-stone-300">
          {run.workload.steps.map((s) => <li key={s}>{s}</li>)}
        </ol>
        {Object.keys(run.workload.budgets).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">{Object.entries(run.workload.budgets).map(([k, val]) => <Pill key={k}>{k} = {val}</Pill>)}</div>
        )}
      </details>
    </div>
  )
}

// --------------------------------------------------------------------------- //
function Violations({ violations, expected }: { violations: Violation[]; expected: string[] }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!violations.length)
    return (
      <div className="card flex items-center gap-2 border-emerald-300 bg-emerald-50/60 p-4 text-sm text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/5 dark:text-emerald-300">
        ✓ 검사기 35종 모두 통과 — 계약 위반이 관찰되지 않았습니다.
      </div>
    )
  return (
    <Section title={`계약 위반 ${violations.length}종`}>
      <ul className="flex flex-col divide-y divide-stone-200 dark:divide-stone-800">
        {violations.map((v) => (
          <li key={v.code} className="py-2.5 first:pt-0 last:pb-0">
            <button className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === v.code ? null : v.code)}>
              <SeverityBadge severity={v.severity} />
              <span className="font-mono text-[13px] font-semibold">{v.code}</span>
              {expected.includes(v.code) && <Pill tone="info">기대</Pill>}
              <span className="flex items-center gap-1 text-xs text-stone-500"><CategoryDot category={v.category} />{v.category}</span>
              <span className="text-xs text-stone-400">×{v.count}</span>
              <span className="ml-auto text-xs text-sky-700 dark:text-sky-400">{open === v.code ? '근거 닫기' : '근거 보기'}</span>
            </button>
            <p className="mt-1 text-sm">{v.title}</p>
            <p className="text-sm text-stone-500 dark:text-stone-400">{v.detail}</p>
            {open === v.code && (
              <pre className="mono mt-2 max-h-72 overflow-auto rounded-md bg-stone-100 p-3 dark:bg-black/30">{JSON.stringify(v.evidence, null, 1)}</pre>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

// --------------------------------------------------------------------------- //
type ChartKey = 'luma' | 'exposure' | 'interval' | 'tsdelta' | 'wb' | 'memory'

function Charts({ run }: { run: RunPayload }) {
  const frames = run.series.frames
  const hasFrames = frames.some((f) => f.points.length > 1)
  const available: { key: ChartKey; label: string }[] = [
    ...(hasFrames
      ? ([
          { key: 'luma', label: '휘도 (AE)' },
          { key: 'exposure', label: '노출 / 프레임 길이' },
          { key: 'interval', label: '결과 도착 간격' },
          { key: 'tsdelta', label: '타임스탬프 Δ' },
          { key: 'wb', label: 'WB 오차' },
        ] as const)
      : []),
    ...(run.series.memory.length ? ([{ key: 'memory', label: 'provider 메모리' }] as const) : []),
  ]
  const [tab, setTab] = useState<ChartKey>(available[0]?.key ?? 'luma')
  const active = available.find((a) => a.key === tab) ? tab : available[0]?.key

  const chart = useMemo(() => {
    const per = (pick: (p: RunPayload['series']['frames'][number]['points'][number]) => number | null): Series[] =>
      frames.map((f, i) => ({ name: f.key, color: PALETTE[i % PALETTE.length], points: f.points.map((p) => ({ x: p.t_ms, y: pick(p) })) }))
    switch (active) {
      case 'luma':
        return { series: per((p) => p.luma), yLabel: 'normalized luma', refLine: { y: 0.18, label: 'AE target 0.18' } }
      case 'exposure':
        return {
          series: [
            ...per((p) => p.exposure_ms).map((s) => ({ ...s, name: `${s.name} exposure` })),
            ...per((p) => p.frame_duration_ms).map((s) => ({ ...s, name: `${s.name} frameDuration`, color: '#a8a29e' })),
          ],
          yLabel: 'ms',
        }
      case 'interval':
        return { series: per((p) => p.interval_ms), yLabel: 'ms between results' }
      case 'tsdelta':
        return { series: per((p) => p.ts_delta_ms), yLabel: 'Δ sensor timestamp (ms, ≤0 = 역전)' }
      case 'wb':
        return { series: per((p) => p.wb_error_k), yLabel: 'K', refLine: { y: 800, label: '허용 800K' } }
      case 'memory':
        return {
          series: [{ name: 'RSS MB', color: '#ea580c', points: run.series.memory.map((m) => ({ x: m.t_ms, y: m.rss_mb })) }],
          yLabel: 'MB',
          refLine: { y: 1900, label: 'LMK 1900MB' },
        }
      default:
        return null
    }
  }, [active, frames, run.series.memory])

  if (!available.length) return null
  return (
    <Section
      title="계측 시계열"
      right={
        <div className="flex flex-wrap gap-1">
          {available.map((a) => (
            <button key={a.key} onClick={() => setTab(a.key)} className={`rounded px-2 py-1 text-xs ${active === a.key ? 'bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900' : 'text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800'}`}>
              {a.label}
            </button>
          ))}
        </div>
      }
    >
      {chart && <LineChart {...chart} />}
      {frames.length > 1 && <p className="mt-1 text-xs text-stone-500">{frames.map((f, i) => <span key={f.key} className="mr-3"><span className="mr-1 inline-block h-2 w-2 rounded-full" style={{ background: PALETTE[i % PALETTE.length] }} />{f.key} ({f.count})</span>)}</p>}
    </Section>
  )
}

// --------------------------------------------------------------------------- //
const LEVEL_STYLE: Record<string, string> = {
  F: 'text-white bg-red-600 px-1 rounded',
  E: 'text-red-600 dark:text-red-400',
  W: 'text-amber-600 dark:text-amber-400',
  I: 'text-stone-700 dark:text-stone-300',
  D: 'text-stone-400',
  V: 'text-stone-400',
}

function LogViewer({ run }: { run: RunPayload }) {
  const [minLevel, setMinLevel] = useState<'all' | 'WEF'>('all')
  const [q, setQ] = useState('')
  const lines = run.logs.filter(
    (l) => (minLevel === 'all' || 'WEF'.includes(l.level)) && (!q || `${l.tag} ${l.msg}`.toLowerCase().includes(q.toLowerCase())),
  )
  return (
    <Section
      title={`logcat ${run.logs_total.toLocaleString()}줄`}
      right={
        <div className="flex flex-wrap items-center gap-2">
          <select className="input h-8" value={minLevel} onChange={(e) => setMinLevel(e.target.value as 'all' | 'WEF')}>
            <option value="all">전체 레벨</option>
            <option value="WEF">W / E / F</option>
          </select>
          <input className="input h-8 w-44" placeholder="tag·메시지 검색" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      }
    >
      {lines.length ? (
        <div className="mono max-h-80 overflow-auto rounded-md bg-stone-50 p-2 dark:bg-black/30">
          {lines.map((l, i) => (
            <div key={i} className="whitespace-pre-wrap break-all">
              <span className="text-stone-400">{(l.t_ms / 1000).toFixed(4).padStart(9)}</span>{' '}
              <span className={LEVEL_STYLE[l.level] ?? ''}>{l.level}</span>{' '}
              <span className="text-sky-700 dark:text-sky-400">{l.tag}</span>: {l.msg}
            </div>
          ))}
        </div>
      ) : (
        <Empty>조건에 맞는 로그가 없습니다.</Empty>
      )}
      {run.logs.length < run.logs_total && <p className="mt-2 text-xs text-stone-500">앞 {run.logs.length.toLocaleString()}줄만 표시 — 전체는 HTML 리포트/JSON 에서 확인</p>}
    </Section>
  )
}

// --------------------------------------------------------------------------- //
const PAGE = 50

function EventBrowser({ run }: { run: RunPayload }) {
  const kinds = run.timeline.lanes.map((l) => l.kind)
  const [kind, setKind] = useState<string>(kinds.includes('result') ? 'result' : (kinds[0] ?? ''))
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<EventPage | null>(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    setPage(null)
    setError(null)
    api.events(run.id, kind || null, offset, PAGE).then((result) => {
      if (alive) setPage(result)
    }).catch((failure) => {
      if (alive) setError(String((failure as Error).message))
    })
    return () => {
      alive = false
    }
  }, [open, run.id, kind, offset])

  return (
    <Section
      title="사건 원본"
      right={
        <button className="btn-ghost h-8" onClick={() => setOpen(!open)}>{open ? '접기' : '펼치기'}</button>
      }
    >
      {!open ? (
        <p className="text-sm text-stone-500">프레임워크가 관찰한 사건(콜백·메타데이터·통계) 원본을 종류별로 조회합니다.</p>
      ) : (
        <>
          {error && <div className="mb-3"><ErrorBox error={error} /></div>}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <select className="input h-8" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0) }}>
              {run.timeline.lanes.map((l) => <option key={l.kind} value={l.kind}>{l.kind} ({l.count})</option>)}
            </select>
            <button className="btn-ghost h-8" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>← 이전</button>
            <button className="btn-ghost h-8" disabled={!page || offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>다음 →</button>
            {page && <span className="text-xs text-stone-500">{offset + 1}–{Math.min(offset + PAGE, page.total)} / {page.total}</span>}
          </div>
          <div className="mono max-h-96 overflow-auto rounded-md bg-stone-50 p-2 dark:bg-black/30">
            {page?.items.map((e, i) => <div key={i} className="border-b border-stone-200/60 py-1 last:border-0 dark:border-stone-800/60">{JSON.stringify(e)}</div>)}
          </div>
        </>
      )}
    </Section>
  )
}
