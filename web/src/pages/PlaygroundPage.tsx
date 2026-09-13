import { useState } from 'react'
import { api, type Catalog, type RunPayload } from '../api'
import { RunView } from '../components/RunView'
import { CategoryDot, Empty, ErrorBox, Field, Section, Spinner } from '../components/ui'

export function PlaygroundPage({ catalog }: { catalog: Catalog }) {
  const [platform, setPlatform] = useState(catalog.platforms[0].code)
  const [workload, setWorkload] = useState('preview')
  const [defects, setDefects] = useState<string[]>([])
  const [seed, setSeed] = useState(7)
  const [run, setRun] = useState<RunPayload | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wl = catalog.workloads.find((w) => w.name === workload)
  const plat = catalog.platforms.find((p) => p.code === platform)

  const toggle = (d: string) => setDefects((cur) => (cur.includes(d) ? cur.filter((x) => x !== d) : [...cur, d]))

  async function execute() {
    setBusy(true)
    setError(null)
    try {
      setRun(await api.run({ platform, workload, defects, seed }))
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Section title="실행 조건">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
          <div className="flex flex-col gap-3">
            <Field label="플랫폼">
              <select className="input" value={platform} onChange={(e) => setPlatform(e.target.value)}>
                {catalog.platforms.map((p) => <option key={p.code} value={p.code}>{p.code} — {p.family}</option>)}
              </select>
            </Field>
            {plat && (
              <p className="-mt-1 text-xs text-stone-500">
                {plat.desc} · ISP {plat.isp} · 포트 {plat.isp_ports} · 풀 {plat.pool_size} · partial {plat.partial_results} · 모드 {plat.sensor_modes.join(', ')}
              </p>
            )}
            <Field label="워크로드">
              <select className="input" value={workload} onChange={(e) => setWorkload(e.target.value)}>
                {catalog.workloads.map((w) => <option key={w.name} value={w.name}>{w.name} — {w.title}</option>)}
              </select>
            </Field>
            {wl && (
              <ol className="-mt-1 list-decimal pl-5 text-xs text-stone-500">
                {wl.steps.map((s) => <li key={s}>{s}</li>)}
              </ol>
            )}
            <div className="flex items-end gap-3">
              <Field label="시드">
                <input className="input w-24" type="number" min={0} value={seed} onChange={(e) => setSeed(Math.max(0, Number(e.target.value) || 0))} />
              </Field>
              <button className="btn-primary" disabled={busy} onClick={execute}>{busy ? <Spinner /> : '▶'} 실행</button>
            </div>
            <ErrorBox error={error} />
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="label">주입할 결함 ({defects.length})</span>
              {defects.length > 0 && <button className="text-xs text-sky-700 dark:text-sky-400" onClick={() => setDefects([])}>모두 해제</button>}
            </div>
            <div className="max-h-80 overflow-auto rounded-md border border-stone-200 p-2 dark:border-stone-800">
              {catalog.categories.map((c) => (
                <div key={c} className="mb-2 last:mb-0">
                  <div className="mb-0.5 flex items-center gap-1 text-[11px] font-semibold text-stone-500"><CategoryDot category={c} />{c}</div>
                  {catalog.faults.filter((f) => f.category === c).map((f) => (
                    <label key={f.id} className="flex cursor-pointer items-start gap-2 rounded px-1 py-0.5 text-[13px] hover:bg-stone-100 dark:hover:bg-stone-800/60">
                      <input type="checkbox" className="mt-1" checked={defects.includes(f.defect)} onChange={() => toggle(f.defect)} />
                      <span className="min-w-0">
                        <span className="font-mono text-[11px] text-stone-500">{f.id}</span> <span className="font-mono text-[12px]">{f.defect}</span>
                        <span className="block truncate text-[11px] text-stone-400">{f.title}</span>
                      </span>
                    </label>
                  ))}
                </div>
              ))}
            </div>
            <p className="mt-1.5 text-xs text-stone-500">결함은 해당 코드 경로를 지나는 워크로드에서만 증상이 드러납니다. 여러 결함을 함께 넣어 상호작용을 볼 수 있습니다.</p>
          </div>
        </div>
      </Section>
      {run ? <RunView key={run.id} run={run} /> : <Empty>조건을 고르고 실행하세요. 결함 없이 실행하면 정상 동작의 기준선을 볼 수 있습니다.</Empty>}
    </div>
  )
}
