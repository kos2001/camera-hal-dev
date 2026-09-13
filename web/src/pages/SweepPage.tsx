import { useState } from 'react'
import { api, type Catalog, type SweepResult } from '../api'
import { CategoryDot, Empty, ErrorBox, Field, Pill, Section, SeverityBadge, Spinner } from '../components/ui'

export function SweepPage({ catalog, onOpenFault }: { catalog: Catalog; onOpenFault: (id: string) => void }) {
  const [seed, setSeed] = useState(7)
  const [result, setResult] = useState<SweepResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function execute() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.sweep(seed))
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  const byCat = result
    ? catalog.categories.map((c) => {
        const rows = result.rows.filter((r) => r.category === c)
        return { c, total: rows.length, ok: rows.filter((r) => r.detected && r.clean).length }
      })
    : []

  return (
    <div className="flex flex-col gap-4">
      <Section title="검증 매트릭스">
        <p className="mb-3 text-sm text-stone-600 dark:text-stone-300">
          결함 {catalog.faults.length}종 각각을 <b>결함 주입</b>과 <b>수정 상태</b>로 같은 절차·시드에서 실행합니다. 주입 시 기대 위반이 모두 검출되고,
          수정 시 위반이 0건이어야 통과입니다. 검사기는 어떤 결함이 들어갔는지 모른 채 관찰 사건만으로 판정합니다.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="시드">
            <input className="input w-24" type="number" min={0} value={seed} onChange={(e) => setSeed(Math.max(0, Number(e.target.value) || 0))} />
          </Field>
          <button className="btn-primary" disabled={busy} onClick={execute}>{busy ? <Spinner /> : '▶'} 전체 스윕 실행 ({catalog.faults.length * 2}회)</button>
        </div>
        <div className="mt-3"><ErrorBox error={error} /></div>
      </Section>

      {!result ? (
        <Empty>{busy ? <Spinner label="74회 시뮬레이션 실행 중…" /> : '스윕을 실행하면 결과가 여기에 표시됩니다.'}</Empty>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="통과" value={`${result.ok} / ${result.total}`} tone={result.ok === result.total ? 'ok' : 'bad'} />
            <Stat label="주입 시 검출" value={`${result.rows.filter((r) => r.detected).length}`} />
            <Stat label="수정 시 오탐 0건" value={`${result.rows.filter((r) => r.clean).length}`} />
          </div>
          <div className="card flex flex-wrap gap-x-4 gap-y-1.5 p-3 text-xs">
            {byCat.map(({ c, ok, total }) => (
              <span key={c} className="inline-flex items-center gap-1.5"><CategoryDot category={c} />{c} <b className={ok === total ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600'}>{ok}/{total}</b></span>
            ))}
          </div>
          <div className="card overflow-x-auto">
            <table className="w-full min-w-[860px] text-sm">
              <thead>
                <tr className="border-b border-stone-200 text-left dark:border-stone-800">
                  {['ID', '분류', '시나리오', '플랫폼 · 워크로드', '결함 주입', '수정 상태'].map((h) => <th key={h} className="label px-3 py-2">{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((r) => (
                  <tr key={r.id} onClick={() => onOpenFault(r.id)} className="cursor-pointer border-b border-stone-100 align-top last:border-0 hover:bg-stone-50 dark:border-stone-800/70 dark:hover:bg-white/[0.03]">
                    <td className="px-3 py-2 font-mono text-xs font-semibold">{r.id}</td>
                    <td className="px-3 py-2 text-xs"><span className="inline-flex items-center gap-1 whitespace-nowrap"><CategoryDot category={r.category} />{r.category}</span><div className="mt-1"><SeverityBadge severity={r.severity} /></div></td>
                    <td className="max-w-[320px] px-3 py-2 text-[13px] leading-snug">{r.title.replace(/^\[[^\]]+\]\s*/, '')}</td>
                    <td className="px-3 py-2 text-xs text-stone-500"><div>{r.platform}</div><div className="font-mono">{r.workload}</div></td>
                    <td className="px-3 py-2">
                      <Pill tone={r.detected ? 'ok' : 'bad'}>{r.detected ? '검출' : '미검출'}</Pill>
                      <div className="mt-1 font-mono text-[11px] text-stone-500">{r.faulty_codes.map((c) => <div key={c} className={r.expect.includes(c) ? 'text-stone-800 dark:text-stone-200' : ''}>{c}</div>)}</div>
                    </td>
                    <td className="px-3 py-2">
                      <Pill tone={r.clean ? 'ok' : 'bad'}>{r.clean ? '위반 0' : `오탐 ${r.fixed_codes.length}`}</Pill>
                      {!r.clean && <div className="mt-1 font-mono text-[11px] text-red-600">{r.fixed_codes.join(', ')}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'ok' | 'bad' }) {
  return (
    <div className="card p-4">
      <div className="label">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${tone === 'ok' ? 'text-emerald-600 dark:text-emerald-400' : tone === 'bad' ? 'text-red-600' : ''}`}>{value}</div>
    </div>
  )
}
