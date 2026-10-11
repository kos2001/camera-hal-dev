import { t, severityLabel, localizeError } from '../i18n'
import { Icon } from './Icon'
import type { ReactNode } from 'react'
import { AUTH_REQUIRED_MESSAGE, loginUrl, type Severity } from '../api'

const SEV_STYLE: Record<Severity, string> = {
  Blocker: 'bg-red-600 text-white',
  Critical: 'bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300',
  Major: 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300',
  Minor: 'bg-stone-200 text-stone-700 dark:bg-stone-700/50 dark:text-stone-300',
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 text-[11px] font-semibold ${SEV_STYLE[severity] ?? SEV_STYLE.Minor}`}>
      {severityLabel(severity)}
    </span>
  )
}

export const CATEGORY_COLOR: Record<string, string> = {
  'Stream Config': '#0ea5e9',
  'Buffer Management': '#8b5cf6',
  'Request Pipeline': '#6366f1',
  Metadata: '#14b8a6',
  '3A Tuning': '#f59e0b',
  'Sensor Driver': '#84cc16',
  'Sync Timing': '#ec4899',
  Performance: '#f97316',
  'HAL Interface': '#06b6d4',
  Security: '#ef4444',
}

export function CategoryDot({ category }: { category: string }) {
  return (
    <span
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{ background: CATEGORY_COLOR[category] ?? '#a8a29e' }}
      aria-hidden
    />
  )
}

export function Pill({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'ok' | 'bad' | 'info' }) {
  const cls = {
    neutral: 'border-stone-300 text-stone-600 dark:border-stone-700 dark:text-stone-300',
    ok: 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-500/40 dark:bg-emerald-500/10 dark:text-emerald-300',
    bad: 'border-red-300 bg-red-50 text-red-800 dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-300',
    info: 'border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-500/40 dark:bg-sky-500/10 dark:text-sky-300',
  }[tone]
  return <span className={`pill inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>{children}</span>
}

export function Section({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <header className="section-heading">
        <h3 className="text-sm font-semibold">{title}</h3>
        {right}
      </header>
      <div className="section-body">{children}</div>
    </section>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-stone-500">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-stone-300 border-t-sky-600" />
      {label}
    </span>
  )
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null
  const needsLogin = error.includes(AUTH_REQUIRED_MESSAGE)
  return (
    <div role="alert" className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-300">
      {localizeError(error)}
      {needsLogin && <a className="ml-2 font-semibold underline" href={loginUrl()}>{t("다시 로그인")}</a>}
    </div>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="label">{label}</span>
      {children}
    </label>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon name="layers" size={21} /></div><div className="empty-content">{children}</div></div>
}
