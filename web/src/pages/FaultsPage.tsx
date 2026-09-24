import { useMemo, useState } from 'react'
import { api, type Catalog, type Fault, type RunPayload } from '../api'
import { RunView } from '../components/RunView'
import { CategoryDot, Empty, ErrorBox, Field, Pill, Section, SeverityBadge, Spinner } from '../components/ui'

type Mode = 'faulty' | 'fixed'

export function FaultsPage({ catalog, selected, onSelect }: { catalog: Catalog; selected: string | null; onSelect: (id: string) => void }) {
  const [q, setQ] = useState('')
  const [cat, setCat] = useState<string | null>(null)
  const fault = catalog.faults.find((f) => f.id === selected) ?? null

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return catalog.faults.filter(
      (f) =>
        (!cat || f.category === cat) &&
        (!needle || [f.id, f.title, f.defect, f.workload, f.platform, ...f.expect].join(' ').toLowerCase().includes(needle)),
    )
  }, [catalog.faults, q, cat])

  return (
    <div className="grid gap-4 lg:grid-cols-[340px_minmax(0,1fr)]">
      <aside className="card flex max-h-none flex-col lg:sticky lg:top-[68px] lg:max-h-[calc(100vh-84px)]">
        <div className="space-y-2 border-b border-stone-200 p-3 dark:border-stone-800">
          <input className="input w-full" placeholder="ID·제목·결함·검출 코드 검색" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="flex flex-wrap gap-1">
            <button onClick={() => setCat(null)} className={`rounded-full border px-2 py-0.5 text-[11px] ${!cat ? 'border-stone-900 bg-stone-900 text-white dark:border-stone-100 dark:bg-stone-100 dark:text-stone-900' : 'border-stone-300 dark:border-stone-700'}`}>전체 {catalog.faults.length}</button>
            {catalog.categories.map((c) => (
              <button key={c} onClick={() => setCat(cat === c ? null : c)} className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${cat === c ? 'border-stone-900 bg-stone-900 text-white dark:border-stone-100 dark:bg-stone-100 dark:text-stone-900' : 'border-stone-300 dark:border-stone-700'}`}>
                <CategoryDot category={c} />
                {c} {catalog.faults.filter((f) => f.category === c).length}
              </button>
            ))}
          </div>
        </div>
        <ul className="min-h-0 flex-1 overflow-auto p-1.5">
          {list.map((f) => (
            <li key={f.id}>
              <button
                onClick={() => onSelect(f.id)}
                className={`w-full rounded-md px-2.5 py-2 text-left transition ${f.id === selected ? 'bg-sky-50 ring-1 ring-sky-300 dark:bg-sky-500/10 dark:ring-sky-500/40' : 'hover:bg-stone-100 dark:hover:bg-stone-800/60'}`}
              >
                <div className="flex items-center gap-1.5 text-[11px] text-stone-500">
                  <span className="font-mono font-semibold text-stone-700 dark:text-stone-300">{f.id}</span>
                  <CategoryDot category={f.category} />
                  {f.category}
                  <span className="ml-auto"><SeverityBadge severity={f.severity} /></span>
                </div>
                <div className="mt-0.5 line-clamp-2 text-[13px] leading-snug">{f.title.replace(/^\[[^\]]+\]\s*/, '')}</div>
                <div className="mt-0.5 truncate text-[11px] text-stone-400">{f.platform}</div>
              </button>
            </li>
          ))}
          {!list.length && <li className="p-4 text-center text-sm text-stone-500">일치하는 결함이 없습니다.</li>}
        </ul>
      </aside>

      <div className="min-w-0">{fault ? <FaultDetail key={fault.id} fault={fault} catalog={catalog} /> : <Empty>왼쪽에서 결함 시나리오를 선택하세요.</Empty>}</div>
    </div>
  )
}

function FaultDetail({ fault, catalog }: { fault: Fault; catalog: Catalog }) {
  const [platform, setPlatform] = useState(fault.platform)
  const [seed, setSeed] = useState(7)
  const [runs, setRuns] = useState<Partial<Record<Mode, RunPayload>>>({})
  const [mode, setMode] = useState<Mode>('faulty')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const workload = catalog.workloads.find((w) => w.name === fault.workload)

  async function compare() {
    setBusy(true)
    setError(null)
    setRuns({})
    setMode('faulty')
    try {
      const res = await api.compare(fault.id, { platform, seed })
      setRuns(res)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const current = runs[mode]

  return (
    <div className="flex flex-col gap-4">
      <div className="card p-4">
        <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
          <span className="font-mono font-semibold text-stone-800 dark:text-stone-200">{fault.id}</span>
          <SeverityBadge severity={fault.severity} />
          <span className="flex items-center gap-1"><CategoryDot category={fault.category} />{fault.category}</span>
          <span>· {fault.family}</span>
        </div>
        <h2 className="mt-1.5 text-lg font-semibold leading-snug">{fault.title}</h2>
        <p className="mt-2 text-sm text-stone-600 dark:text-stone-300">{fault.symptom}</p>

        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <Info title="근본 원인">{fault.root_cause}</Info>
          <Info title="해결책">{fault.resolution}</Info>
          <Info title="임시 우회책">{fault.workaround}</Info>
          <Info title="시뮬레이터 매핑">
            <div className="flex flex-col gap-1 text-[13px]">
              <span>결함 스위치 <code className="rounded bg-stone-100 px-1 font-mono dark:bg-stone-800">{fault.defect}</code></span>
              <span>워크로드 <code className="rounded bg-stone-100 px-1 font-mono dark:bg-stone-800">{fault.workload}</code> — {workload?.title}</span>
              <span className="flex flex-wrap items-center gap-1">기대 검출 {fault.expect.map((c) => <Pill key={c} tone="info">{c}</Pill>)}</span>
            </div>
          </Info>
        </div>
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-medium text-sky-700 dark:text-sky-400">참고 이슈의 로그 발췌</summary>
          <pre className="mono mt-2 overflow-auto rounded-md bg-stone-100 p-3 dark:bg-black/30">{fault.log_excerpt}</pre>
        </details>
      </div>

      <Section title="재현 실행">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="플랫폼">
            <select className="input min-w-56" value={platform} onChange={(e) => setPlatform(e.target.value)}>
              {catalog.platforms.map((p) => (
                <option key={p.code} value={p.code}>{p.code}{p.code === fault.platform ? ' (기본)' : ''}</option>
              ))}
            </select>
          </Field>
          <Field label="시드">
            <input className="input w-24" type="number" min={0} value={seed} onChange={(e) => setSeed(Math.max(0, Number(e.target.value) || 0))} />
          </Field>
          <button className="btn-primary" disabled={busy} onClick={compare}>
            {busy ? <Spinner /> : '▶'} 결함 주입 vs 수정 상태 실행
          </button>
          {platform !== fault.platform && <span className="text-xs text-amber-700 dark:text-amber-400">다른 벤더 스택에 같은 결함을 이식해 실행합니다.</span>}
        </div>
        <ErrorBox error={error} />
        {(runs.faulty || runs.fixed) && (
          <div className="mt-4 grid gap-2 sm:grid-cols-2">
            {(['faulty', 'fixed'] as Mode[]).map((m) => {
              const r = runs[m]
              if (!r) return null
              const good = m === 'faulty' ? r.verdict?.detected : r.verdict?.clean
              return (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`rounded-lg border p-3 text-left transition ${mode === m ? 'border-sky-500 ring-2 ring-sky-500/20' : 'border-stone-200 hover:border-stone-400 dark:border-stone-700'}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold">{m === 'faulty' ? '⚡ 결함 주입' : '✓ 수정 상태'}</span>
                    <Pill tone={good ? 'ok' : 'bad'}>{m === 'faulty' ? (good ? '검출 성공' : '미검출') : good ? '위반 0건' : '오탐 발생'}</Pill>
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {r.violations.length ? r.violations.map((v) => <span key={v.code} className="font-mono text-[11px] text-stone-600 dark:text-stone-300">{v.code}</span>).reduce<React.ReactNode[]>((acc, el, i) => (i ? [...acc, <span key={`s${i}`} className="text-stone-300">·</span>, el] : [el]), []) : <span className="text-xs text-stone-500">위반 없음</span>}
                  </div>
                  <div className="mt-1 text-[11px] text-stone-400">results {r.counters.result ?? 0} · logs {r.logs_total} · sim {(r.sim_ms / 1000).toFixed(1)}s</div>
                </button>
              )
            })}
          </div>
        )}
      </Section>

      {current ? <RunView key={current.id} run={current} /> : busy
        ? <Empty><Spinner label="시뮬레이션 실행 중…" /></Empty>
        : <Empty>조건을 확인한 뒤 비교 실행을 누르면 결함 주입과 수정 상태의 결과가 기록에 저장됩니다.</Empty>}
    </div>
  )
}

function Info({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md bg-stone-50 p-3 dark:bg-white/[0.03]">
      <div className="label mb-1">{title}</div>
      <div className="text-sm leading-relaxed text-stone-700 dark:text-stone-300">{children}</div>
    </div>
  )
}
