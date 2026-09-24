import { useEffect, useState } from 'react'
import { api, type RunPayload, type RunSummary } from '../api'
import { RunView } from '../components/RunView'
import { Empty, ErrorBox, Pill, Spinner } from '../components/ui'

const PAGE_SIZE = 50

export function HistoryPage({ selected, onSelect, onClear }: {
  selected: string | null
  onSelect: (id: string) => void
  onClear: () => void
}) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [run, setRun] = useState<RunPayload | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(true)
  const [moreBusy, setMoreBusy] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<number | undefined>()
  const [runBusy, setRunBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [clearBusy, setClearBusy] = useState(false)

  useEffect(() => {
    let active = true
    api.runs(PAGE_SIZE).then((rows) => {
      if (active) {
        setRuns(rows)
        setHasMore(rows.length === PAGE_SIZE)
        setNextCursor(rows.at(-1)?.cursor)
      }
    }).catch((e) => {
      if (active) setListError(String((e as Error).message))
    }).finally(() => {
      if (active) setListBusy(false)
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!selected) {
      setRun(null)
      setRunError(null)
      setRunBusy(false)
      return
    }
    let active = true
    setRun(null)
    setRunError(null)
    setRunBusy(true)
    api.getRun(selected).then((result) => {
      if (active) setRun(result)
    }).catch((e) => {
      if (active) setRunError(String((e as Error).message))
    }).finally(() => {
      if (active) setRunBusy(false)
    })
    return () => { active = false }
  }, [selected])

  async function loadMore() {
    if (nextCursor === undefined || moreBusy) return
    setMoreBusy(true)
    setListError(null)
    try {
      const rows = await api.runs(PAGE_SIZE, nextCursor)
      setRuns((current) => [...current, ...rows])
      setHasMore(rows.length === PAGE_SIZE)
      setNextCursor(rows.at(-1)?.cursor)
    } catch (e) {
      setListError(String((e as Error).message))
    } finally {
      setMoreBusy(false)
    }
  }

  async function deleteSelected() {
    if (!selected || deleteBusy || clearBusy ||
      !window.confirm('이 실행 기록을 서비스에서 삭제할까요? 백업 사본에는 보존 기간 동안 남을 수 있습니다.')) return
    setDeleteBusy(true)
    setRunError(null)
    try {
      await api.deleteRun(selected)
      setRuns((current) => current.filter((item) => item.id !== selected))
      setRun(null)
      onClear()
    } catch (e) {
      setRunError(String((e as Error).message))
    } finally {
      setDeleteBusy(false)
    }
  }

  async function clearHistory() {
    if (clearBusy || deleteBusy || listBusy || (runs.length === 0 && !hasMore) ||
      !window.confirm('내 실행 기록을 모두 삭제할까요? 백업 사본에는 보존 기간 동안 남을 수 있습니다.')) return
    setClearBusy(true)
    setListError(null)
    try {
      await api.deleteAllRuns()
      setRuns([])
      setRun(null)
      setHasMore(false)
      setNextCursor(undefined)
      onClear()
    } catch (e) {
      setListError(String((e as Error).message))
    } finally {
      setClearBusy(false)
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
      <aside className="card flex max-h-none flex-col lg:sticky lg:top-[68px] lg:max-h-[calc(100vh-84px)]">
        <div className="border-b border-stone-200 p-3 dark:border-stone-800">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold">실행 기록</h2>
            {(runs.length > 0 || hasMore) && <button className="text-xs text-red-700 hover:underline disabled:opacity-50 dark:text-red-400"
              disabled={clearBusy || deleteBusy || moreBusy} onClick={clearHistory}>
              {clearBusy ? '삭제 중…' : '전체 삭제'}
            </button>}
          </div>
          <p className="mt-1 text-xs text-stone-500">최신 순 · 서버에 저장된 결과</p>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-1.5">
          {listBusy && <div className="p-3"><Spinner label="기록 불러오는 중…" /></div>}
          <ErrorBox error={listError} />
          {!listBusy && !listError && runs.length === 0 && !hasMore && <p className="p-3 text-sm text-stone-500">저장된 실행이 없습니다.</p>}
          <ul>
            {runs.map((item) => (
              <li key={item.id}>
                <button onClick={() => onSelect(item.id)}
                  className={`w-full rounded-md px-2.5 py-2 text-left transition ${item.id === selected ? 'bg-sky-50 ring-1 ring-sky-300 dark:bg-sky-500/10 dark:ring-sky-500/40' : 'hover:bg-stone-100 dark:hover:bg-stone-800/60'}`}>
                  <span className="flex items-center gap-2 text-xs font-semibold">
                    {item.fault ?? item.workload}
                    <span className="ml-auto"><Pill tone={item.violations ? 'bad' : 'ok'}>{item.violations} 위반</Pill></span>
                  </span>
                  <span className="mt-1 block truncate text-xs text-stone-500">{item.platform} · seed {item.seed}</span>
                  <span className="mt-1 block text-[11px] text-stone-400">{item.created_at ? new Date(item.created_at).toLocaleString() : item.id.slice(0, 12)}</span>
                </button>
              </li>
            ))}
          </ul>
          {hasMore && (
            <button className="btn-ghost mt-2 w-full" disabled={moreBusy} onClick={loadMore}>
              {moreBusy ? <Spinner label="더 불러오는 중…" /> : '이전 기록 더 보기'}
            </button>
          )}
        </div>
      </aside>
      <div className="min-w-0">
        {runBusy && <Empty><Spinner label="결과 불러오는 중…" /></Empty>}
        <ErrorBox error={runError} />
        {!runBusy && !runError && run && <>
          <div className="mb-3 flex justify-end">
            <button className="btn-ghost text-red-700 dark:text-red-400" disabled={deleteBusy} onClick={deleteSelected}>
              {deleteBusy ? '삭제 중…' : '기록 삭제'}
            </button>
          </div>
          <RunView key={run.id} run={run} />
        </>}
        {!selected && <Empty>왼쪽에서 실행 기록을 선택하세요.</Empty>}
      </div>
    </div>
  )
}
