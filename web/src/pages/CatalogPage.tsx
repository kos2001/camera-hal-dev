import { useState } from 'react'
import type { Catalog } from '../api'
import { CategoryDot, Pill, SeverityBadge } from '../components/ui'

type Tab = 'platforms' | 'workloads' | 'checkers'

export function CatalogPage({ catalog }: { catalog: Catalog }) {
  const [tab, setTab] = useState<Tab>('platforms')
  const tabs: { key: Tab; label: string }[] = [
    { key: 'platforms', label: `플랫폼 ${catalog.platforms.length}` },
    { key: 'workloads', label: `워크로드 ${catalog.workloads.length}` },
    { key: 'checkers', label: `검사기 ${catalog.checkers.length}` },
  ]
  const faultsByWorkload = (name: string) => catalog.faults.filter((f) => f.workload === name).map((f) => f.id)
  const faultsByCode = (code: string) => catalog.faults.filter((f) => f.expect.includes(code)).map((f) => f.id)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} className={`rounded-md px-3 py-1.5 text-sm ${tab === t.key ? 'bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900' : 'text-stone-600 hover:bg-stone-200/60 dark:text-stone-300 dark:hover:bg-stone-800'}`}>{t.label}</button>
        ))}
      </div>

      {tab === 'platforms' && (
        <div className="grid gap-3 md:grid-cols-2">
          {catalog.platforms.map((p) => (
            <div key={p.code} className="card p-4">
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="font-mono text-sm font-semibold">{p.code}</h3>
                <span className="text-xs text-stone-500">{p.family}</span>
              </div>
              <p className="mt-1 text-sm text-stone-600 dark:text-stone-300">{p.desc}</p>
              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                <dt className="text-stone-500">ISP</dt><dd>{p.isp} · 출력 포트 {p.isp_ports} · partial {p.partial_results}</dd>
                <dt className="text-stone-500">센서 모드</dt><dd className="font-mono">{p.sensor_modes.join('  ')}</dd>
                <dt className="text-stone-500">최대 BLOB</dt><dd className="font-mono">{p.max_blob}</dd>
                <dt className="text-stone-500">버퍼 풀</dt><dd>스트림당 {p.pool_size}</dd>
                <dt className="text-stone-500">카메라</dt><dd>{p.camera_ids.join(', ')}{p.logical_multicam ? ' · 논리 멀티카메라' : ''}{p.external_camera ? ' · 외장 USB' : ''}{p.channels > 1 ? ` · ${p.channels}채널` : ''}</dd>
                <dt className="text-stone-500">호스트</dt><dd>{p.host_platforms.join(' / ')}</dd>
              </dl>
            </div>
          ))}
        </div>
      )}

      {tab === 'workloads' && (
        <div className="card divide-y divide-stone-200 dark:divide-stone-800">
          {catalog.workloads.map((w) => (
            <div key={w.name} className="grid gap-2 p-3 md:grid-cols-[260px_minmax(0,1fr)]">
              <div>
                <div className="font-mono text-[13px] font-semibold">{w.name}</div>
                <div className="text-sm">{w.title}</div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {faultsByWorkload(w.name).map((id) => <Pill key={id} tone="info">{id}</Pill>)}
                  {Object.entries(w.budgets).map(([k, v]) => <Pill key={k}>{k}={v}</Pill>)}
                </div>
              </div>
              <ol className="list-decimal pl-5 text-sm text-stone-600 dark:text-stone-300">{w.steps.map((s) => <li key={s}>{s}</li>)}</ol>
            </div>
          ))}
        </div>
      )}

      {tab === 'checkers' && (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-stone-200 text-left dark:border-stone-800">
                {['코드', '분류', '심각도', '판정 내용', '검출 대상 결함'].map((h) => <th key={h} className="label px-3 py-2">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {catalog.checkers.map((c) => (
                <tr key={c.code} className="border-b border-stone-100 last:border-0 dark:border-stone-800/70">
                  <td className="px-3 py-2 font-mono text-xs font-semibold">{c.code}</td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs"><span className="inline-flex items-center gap-1"><CategoryDot category={c.category} />{c.category}</span></td>
                  <td className="px-3 py-2"><SeverityBadge severity={c.severity} /></td>
                  <td className="px-3 py-2">{c.title}</td>
                  <td className="px-3 py-2 font-mono text-xs text-stone-500">{faultsByCode(c.code).join(' ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
