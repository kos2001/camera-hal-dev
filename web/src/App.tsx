import { useEffect, useState } from 'react'
import { api, type Catalog } from './api'
import { ErrorBox, Spinner } from './components/ui'
import { CatalogPage } from './pages/CatalogPage'
import { FaultsPage } from './pages/FaultsPage'
import { HistoryPage } from './pages/HistoryPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { SweepPage } from './pages/SweepPage'

type Page = 'faults' | 'run' | 'sweep' | 'history' | 'catalog'

interface Route {
  page: Page
  arg: string | null
}

function parseHash(): Route {
  const [, page, arg] = window.location.hash.replace(/^#/, '').split('/')
  const valid: Page[] = ['faults', 'run', 'sweep', 'history', 'catalog']
  return { page: valid.includes(page as Page) ? (page as Page) : 'faults', arg: arg ?? null }
}

const NAV: { page: Page; label: string }[] = [
  { page: 'faults', label: '결함 시나리오' },
  { page: 'run', label: '자유 실행' },
  { page: 'sweep', label: '검증 매트릭스' },
  { page: 'history', label: '실행 기록' },
  { page: 'catalog', label: '카탈로그' },
]

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadCatalog = () => {
    setError(null)
    api.catalog().then(setCatalog).catch((e) => setError(`카탈로그를 불러오지 못했습니다: ${e.message}`))
  }

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    loadCatalog()
  }, [])

  const go = (page: Page, arg?: string) => {
    window.location.hash = `/${page}${arg ? `/${arg}` : ''}`
  }

  const selectedFault = route.page === 'faults' ? (route.arg ?? catalog?.faults[0]?.id ?? null) : null

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-stone-200 bg-stone-50/90 backdrop-blur dark:border-stone-800 dark:bg-[#111316]/90">
        <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4">
          <a href="#/faults" className="flex items-center gap-2 font-semibold">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-stone-900 text-sm text-white dark:bg-stone-100 dark:text-stone-900">◉</span>
            <span>camhal-sim</span>
            <span className="hidden text-xs font-normal text-stone-500 sm:inline">Camera HAL3 시뮬레이터 {catalog ? `v${catalog.version}` : ''}</span>
          </a>
          <nav className="flex gap-1 overflow-x-auto">
            {NAV.map((n) => (
              <button
                key={n.page}
                onClick={() => go(n.page)}
                className={`whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition ${route.page === n.page ? 'bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900' : 'text-stone-600 hover:bg-stone-200/60 dark:text-stone-300 dark:hover:bg-stone-800'}`}
              >
                {n.label}
              </button>
            ))}
          </nav>
          <a className="ml-auto hidden text-xs text-stone-500 hover:text-stone-800 md:inline dark:hover:text-stone-200" href="/docs" target="_blank" rel="noreferrer">API 문서 ↗</a>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-4 py-4">
        {error && <div className="space-y-2">
          <ErrorBox error={error} />
          <button className="btn-ghost" onClick={loadCatalog}>다시 시도</button>
        </div>}
        {!catalog && !error && <div className="py-20 text-center"><Spinner label="카탈로그 불러오는 중…" /></div>}
        {catalog && route.page === 'faults' && <FaultsPage catalog={catalog} selected={selectedFault} onSelect={(id) => go('faults', id)} />}
        {catalog && route.page === 'run' && <PlaygroundPage catalog={catalog} />}
        {catalog && route.page === 'sweep' && <SweepPage catalog={catalog} onOpenFault={(id) => go('faults', id)} />}
        {catalog && route.page === 'history' && <HistoryPage selected={route.arg} onSelect={(id) => go('history', id)} onClear={() => go('history')} />}
        {catalog && route.page === 'catalog' && <CatalogPage catalog={catalog} />}
      </main>
    </div>
  )
}
