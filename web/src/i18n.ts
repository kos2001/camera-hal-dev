import { useSyncExternalStore } from 'react'
import messages from './locales/en.json'

export type Locale = 'ko' | 'en'
const listeners = new Set<() => void>()
function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem('camhal.locale')
    if (saved === 'ko' || saved === 'en') return saved
  } catch { /* Storage may be disabled. */ }
  return navigator.language.toLowerCase().startsWith('ko') ? 'ko' : 'en'
}
let locale = initialLocale()
export function setLocale(value: Locale) {
  locale = value
  try { localStorage.setItem('camhal.locale', value) } catch { /* Keep the current session usable. */ }
  listeners.forEach((listener) => listener())
}
export function useLocale() {
  return useSyncExternalStore((listener) => { listeners.add(listener); return () => listeners.delete(listener) }, () => locale)
}
export function t(key: string, ...values: (string | number)[]): string {
  const translated = locale === 'en' ? (messages as Record<string, string>)[key] ?? key : key
  return translated.replace(/\{(\d+)\}/g, (_, index: string) => String(values[Number(index)] ?? `{${index}}`))
}

const categoryNames: Record<string, string> = {
  'Stream Config': '스트림 구성', 'Buffer Management': '버퍼 관리', 'Request Pipeline': '요청 파이프라인', Metadata: '메타데이터', '3A Tuning': '3A 튜닝', 'Sensor Driver': '센서 드라이버', 'Sync Timing': '타이밍 동기화', Performance: '성능', 'HAL Interface': 'HAL 인터페이스', Security: '보안',
}
export const categoryLabel = (value: string) => t(categoryNames[value] ?? value)
export const severityLabel = (value: string) => t(({ Blocker: '차단', Critical: '심각', Major: '주요', Minor: '경미' } as Record<string, string>)[value] ?? value)

export function localizeError(message: string): string {
  for (const prefix of ['실행 결과 없음(만료되었을 수 있음): ', '알 수 없는 결함: ']) {
    if (message.startsWith(prefix)) return t(`${prefix}{0}`, message.slice(prefix.length))
  }
  return t(message)
}
