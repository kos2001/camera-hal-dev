import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type Catalog } from './api'
import { ErrorBox, Spinner } from './components/ui'
import { Icon, type IconName } from './components/Icon'
import { CatalogPage } from './pages/CatalogPage'
import { FaultsPage } from './pages/FaultsPage'
import { HistoryPage } from './pages/HistoryPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { SweepPage } from './pages/SweepPage'
import { setLocale, t, useLocale } from './i18n'
import { localizeCatalog } from './locales/catalog-en'

type Page = 'faults' | 'run' | 'sweep' | 'history' | 'catalog'
interface Route { page: Page; arg: string | null }
const NAV: { page: Page; label: string; icon: IconName; title: string; description: string }[] = [
  { page: 'faults', label: '결함 시나리오', icon: 'scenarios', title: '카메라 스택의 문제를 이해하고, 해결을 검증하세요.', description: '실제 HAL 코드 경로를 모델링한 시나리오를 탐색하고 결함 주입 전후를 비교하세요.' },
  { page: 'run', label: '자유 실행', icon: 'play', title: '자유롭게 구성하고, 가설을 실험하세요.', description: '플랫폼과 워크로드를 선택하고 여러 결함의 상호작용을 확인하세요.' },
  { page: 'sweep', label: '검증 매트릭스', icon: 'matrix', title: '모든 시나리오를 한 번에 검증하세요.', description: '동일한 조건의 결함 주입·수정 실행으로 검출과 오탐을 확인하세요.' },
  { page: 'history', label: '실행 기록', icon: 'history', title: '실험의 모든 기록, 한곳에서.', description: '저장된 실행을 다시 열어 결과를 살펴보고 팀과 공유하세요.' },
  { page: 'catalog', label: '카탈로그', icon: 'catalog', title: '카메라 스택을 구성하는 요소를 살펴보세요.', description: '플랫폼의 능력, 워크로드 절차, 계약 검사 기준을 확인하세요.' },
]
function parseHash(): Route {
  const [, page, arg] = window.location.hash.replace(/^#/, '').split('/')
  return { page: NAV.some((n) => n.page === page) ? page as Page : 'faults', arg: arg ?? null }
}
function initialTheme(): boolean {
  try { const saved = localStorage.getItem('camhal.theme'); if (saved) return saved === 'dark' } catch { /* Use system preference. */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}
export default function App() {
  const locale = useLocale()
  const [route, setRoute] = useState<Route>(parseHash)
  const [rawCatalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [online, setOnline] = useState<boolean | null>(null)
  const [dark, setDark] = useState(initialTheme)
  const [menu, setMenu] = useState(false)
  const sidebarRef = useRef<HTMLElement>(null)
  const catalog = useMemo(() => rawCatalog ? localizeCatalog(rawCatalog, locale) : null, [rawCatalog, locale])
  const currentPage = NAV.find((n) => n.page === route.page)!
  const loadCatalog = () => {
    setError(null)
    api.catalog().then(setCatalog).catch((e) => setError(String(e.message)))
  }
  useEffect(() => {
    const onHash = () => { setRoute(parseHash()); setMenu(false) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => { loadCatalog() }, [])
  useEffect(() => {
    document.documentElement.lang = locale
    document.title = `camhal studio · ${t(currentPage.label)}`
  }, [locale, currentPage.label])
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try { localStorage.setItem('camhal.theme', dark ? 'dark' : 'light') } catch { /* The toggle still works without storage. */ }
  }, [dark])
  useEffect(() => {
    let active = true
    const check = () => fetch('/api/ready').then((r) => { if (active) setOnline(r.ok) }).catch(() => { if (active) setOnline(false) })
    check(); const timer = window.setInterval(check, 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => {
    if (!menu) return
    const sidebar = sidebarRef.current
    const trigger = document.querySelector<HTMLButtonElement>('.mobile-menu')
    sidebar?.querySelector<HTMLButtonElement>('.sidebar-close')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenu(false)
      if (e.key === 'Tab' && sidebar) {
        const items = Array.from(sidebar.querySelectorAll<HTMLElement>('a[href], button:not(:disabled)'))
        const first = items[0], last = items.at(-1)
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); trigger?.focus() }
  }, [menu])
  const go = (page: Page, arg?: string) => { window.location.hash = `/${page}${arg ? `/${arg}` : ''}`; setMenu(false) }
  const selectedFault = route.page === 'faults' ? (route.arg ?? catalog?.faults[0]?.id ?? null) : null
  const metrics: { label: string; value: number; icon: IconName; detail: string }[] = catalog ? [
    { label: '결함 시나리오 수', value: catalog.faults.length, icon: 'scenarios', detail: 'FAULT SCENARIOS' },
    { label: '대상 플랫폼 수', value: catalog.platforms.length, icon: 'chip', detail: 'PLATFORM PROFILES' },
    { label: '계약 검사기 수', value: catalog.checkers.length, icon: 'shield', detail: 'CONTRACT CHECKERS' },
    { label: '고장 분류 수', value: catalog.categories.length, icon: 'layers', detail: 'FAULT CATEGORIES' },
  ] : []
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">{t('본문으로 이동')}</a>
      {menu && <button className="sidebar-scrim" tabIndex={-1} aria-label={t('메뉴 닫기')} onClick={() => setMenu(false)} />}
      <aside ref={sidebarRef} className={`app-sidebar ${menu ? 'is-open' : ''}`}>
        <button className="sidebar-close" aria-label={t('메뉴 닫기')} onClick={() => setMenu(false)}><Icon name="close" size={18} /></button>
        <a href="#/faults" className="brand" onClick={() => setMenu(false)}>
          <span className="brand-mark"><span /><span /><span /><span /></span>
          <span>camhal<span className="brand-studio">studio</span></span>
        </a>
        <div className="workspace-selector"><span className="workspace-avatar">C</span><div><strong>Camera HAL3</strong><span>{t('시뮬레이션 워크스페이스')}</span></div><span className="workspace-version">v{catalog?.version ?? '0.1'}</span></div>
        <div className="nav-label">{t('탐색')}</div>
        <nav aria-label={t('탐색')} className="side-nav">
          {NAV.map((n) => <a key={n.page} href={`#/${n.page}`} onClick={() => setMenu(false)} aria-current={route.page === n.page ? 'page' : undefined} className={route.page === n.page ? 'active' : ''}><Icon name={n.icon} size={19} /><span>{t(n.label)}</span>{n.page === 'faults' && catalog && <span className="nav-count">{catalog.faults.length}</span>}</a>)}
        </nav>
        <div className="sidebar-bottom">
          <div className="nav-label">{t('리소스')}</div>
          <a className="resource-link" href="/docs" target="_blank" rel="noreferrer"><Icon name="book" size={18} />{t('문서 보기')}<span>↗</span></a>
          <div className="sidebar-environment"><span className="status-dot" /><div><strong>{t('개발 환경')}</strong><span>Local workspace</span></div><span className="env-badge">DEV</span></div>
        </div>
      </aside>
      <div className="app-content">
        <header className="app-topbar">
          <div className="flex min-w-0 items-center gap-3"><button className="icon-button mobile-menu" aria-label={t(menu ? '메뉴 닫기' : '메뉴 열기')} aria-expanded={menu} onClick={() => setMenu(!menu)}><Icon name={menu ? 'close' : 'menu'} /></button><span className="breadcrumb-root">Workspace</span><Icon name="chevron" size={13} /><span className="breadcrumb-page">{t(currentPage.label)}</span></div>
          <div className="topbar-actions">
            <span className={`service-status ${online === false ? 'offline' : ''}`} role="status"><span className="status-dot" />{t(online === null ? '연결 확인 중' : online ? '서비스 연결됨' : '서비스 연결 실패')}</span>
            <div className="language-switch" role="group" aria-label={t('언어 선택')}><Icon name="globe" size={15} /><button lang="ko" aria-pressed={locale === 'ko'} onClick={() => setLocale('ko')}>KO</button><button lang="en" aria-pressed={locale === 'en'} onClick={() => setLocale('en')}>EN</button></div>
            <button className="icon-button theme-button" aria-label={t(dark ? '밝은 테마' : '어두운 테마')} onClick={() => setDark(!dark)}><Icon name={dark ? 'sun' : 'moon'} size={18} /></button>
            {catalog?.auth_mode === 'oidc' && <a className="text-xs" href="/oauth2/sign_out?rd=%2Fsigned-out">{t('이 앱에서 로그아웃')}</a>}
          </div>
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>
          <div className="page-intro">
            <div><div className="eyebrow"><span />CAMERA HAL3 SIMULATION</div><h1>{t(currentPage.label)}</h1><p className="intro-title">{t(currentPage.title)}</p><p className="intro-description">{t(currentPage.description)}</p></div>
            {route.page !== 'run' && <button className="btn-primary new-run" onClick={() => go('run')}><Icon name="play" size={15} />{t('새 시뮬레이션')}<Icon name="arrow" size={16} /></button>}
          </div>
          {route.page === 'faults' && <div className="overview-metrics">{metrics.map((m) => <div key={m.label} className="metric-card"><div className="metric-top"><span>{t(m.label)}</span><Icon name={m.icon} size={19} /></div><div className="metric-value">{m.value.toString().padStart(2, '0')}<span>{m.detail}</span></div></div>)}</div>}
          {error && <div className="card space-y-3 p-5"><ErrorBox error={error} /><button className="btn-ghost" onClick={loadCatalog}>{t('다시 시도')}</button></div>}
          {!catalog && !error && <div className="card py-20 text-center"><Spinner label={t('카탈로그 불러오는 중…')} /></div>}
          {catalog && route.page === 'faults' && <FaultsPage catalog={catalog} selected={selectedFault} onSelect={(id) => go('faults', id)} />}
          {catalog && route.page === 'run' && <PlaygroundPage catalog={catalog} />}
          {catalog && route.page === 'sweep' && <SweepPage catalog={catalog} onOpenFault={(id) => go('faults', id)} />}
          {catalog && route.page === 'history' && <HistoryPage selected={route.arg} onSelect={(id) => go('history', id)} onClear={() => go('history')} />}
          {catalog && route.page === 'catalog' && <CatalogPage catalog={catalog} />}
          <footer className="app-footer"><span>camhal studio <span className="mx-2">/</span> Camera HAL3 Simulator</span><span>Deterministic by design <span className="footer-dot">·</span> v{catalog?.version ?? '0.1'}</span></footer>
        </main>
      </div>
    </div>
  )
}
