import { t } from '../i18n'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Lane } from '../api'

export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(800)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, Math.floor(entry.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width] as const
}

function niceTicks(max: number, n = 5): number[] {
  if (max <= 0) return [0]
  const raw = max / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out: number[] = []
  for (let v = 0; v <= max + 1e-9; v += step) out.push(v)
  return out
}

const fmtMs = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s` : `${Math.round(ms)}ms`)

// --------------------------------------------------------------------------- //
// 사건 타임라인 (레인별 틱)
// --------------------------------------------------------------------------- //
export function Timeline({ lanes, tEnd }: { lanes: Lane[]; tEnd: number }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<{ x: number; t: number } | null>(null)
  const left = 150
  const right = 12
  const laneH = 20
  const plotW = width - left - right
  const h = lanes.length * laneH + 26
  const x = (t: number) => left + (plotW * t) / Math.max(tEnd, 1)
  const ticks = niceTicks(tEnd, Math.max(3, Math.floor(plotW / 110)))

  return (
    <div ref={ref} className="relative w-full select-none">
      <svg
        width={width}
        height={h}
        role="img"
        aria-label={t("사건 타임라인")}
        onMouseMove={(e) => {
          const bx = e.currentTarget.getBoundingClientRect().left
          const px = e.clientX - bx
          if (px < left || px > left + plotW) return setHover(null)
          setHover({ x: px, t: ((px - left) / plotW) * tEnd })
        }}
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={0} y2={h - 18} className="stroke-stone-200 dark:stroke-stone-800" />
            <text x={x(t)} y={h - 4} textAnchor="middle" className="fill-stone-500 text-[10px]">
              {fmtMs(t)}
            </text>
          </g>
        ))}
        {lanes.map((lane, i) => {
          const y = i * laneH
          return (
            <g key={lane.kind}>
              <text x={4} y={y + 14} className={`text-[11px] ${lane.bad ? 'fill-red-600 dark:fill-red-400' : 'fill-stone-600 dark:fill-stone-400'}`}>
                {lane.kind} <tspan className="fill-stone-400">{lane.count}</tspan>
              </text>
              <line x1={left} x2={left + plotW} y1={y + 10} y2={y + 10} className="stroke-stone-100 dark:stroke-stone-800/60" />
              {lane.points.map((t, j) => (
                <rect
                  key={j}
                  x={x(t) - 0.75}
                  y={y + 4}
                  width={1.5}
                  height={12}
                  className={lane.bad ? 'fill-red-500' : 'fill-sky-600 dark:fill-sky-400'}
                  opacity={lane.points.length > 300 ? 0.55 : 0.9}
                />
              ))}
            </g>
          )
        })}
        {hover && <line x1={hover.x} x2={hover.x} y1={0} y2={h - 18} className="stroke-stone-400" strokeDasharray="3 3" />}
      </svg>
      {hover && (
        <div
          className="pointer-events-none absolute top-0 rounded bg-stone-900 px-1.5 py-0.5 text-[11px] text-white dark:bg-stone-100 dark:text-stone-900"
          style={{ left: Math.min(hover.x + 6, width - 70) }}
        >
          {hover.t.toFixed(1)} ms
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 선 그래프
// --------------------------------------------------------------------------- //
export interface Series {
  name: string
  color: string
  points: { x: number; y: number | null }[]
}

export function LineChart({
  series,
  yLabel,
  height = 180,
  refLine,
  xLabel = 'sim time',
}: {
  series: Series[]
  yLabel: string
  height?: number
  refLine?: { y: number; label: string }
  xLabel?: string
}) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hoverX, setHoverX] = useState<number | null>(null)
  const pad = { l: 48, r: 12, t: 10, b: 24 }
  const plotW = width - pad.l - pad.r
  const plotH = height - pad.t - pad.b

  const { xMax, xMin, yMax } = useMemo(() => {
    let xMin = Infinity
    let xMax = -Infinity
    let yMax = refLine?.y ?? 0
    for (const s of series)
      for (const p of s.points) {
        xMin = Math.min(xMin, p.x)
        xMax = Math.max(xMax, p.x)
        if (p.y != null) yMax = Math.max(yMax, p.y)
      }
    return { xMin: Number.isFinite(xMin) ? xMin : 0, xMax: Number.isFinite(xMax) ? xMax : 1, yMax: yMax * 1.08 || 1 }
  }, [series, refLine])

  const sx = (v: number) => pad.l + ((v - xMin) / Math.max(xMax - xMin, 1e-9)) * plotW
  const sy = (v: number) => pad.t + plotH - (v / yMax) * plotH
  const yTicks = niceTicks(yMax, 4)
  const xTicks = niceTicks(xMax - xMin, Math.max(3, Math.floor(plotW / 110))).map((t) => t + xMin)

  const hoverVals =
    hoverX == null
      ? null
      : series.map((s) => {
          let best = s.points[0]
          for (const p of s.points) if (Math.abs(p.x - hoverX) < Math.abs((best?.x ?? Infinity) - hoverX)) best = p
          return { name: s.name, color: s.color, p: best }
        })

  return (
    <div ref={ref} className="relative w-full">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={yLabel}
        onMouseMove={(e) => {
          const px = e.clientX - e.currentTarget.getBoundingClientRect().left
          if (px < pad.l || px > pad.l + plotW) return setHoverX(null)
          setHoverX(xMin + ((px - pad.l) / plotW) * (xMax - xMin))
        }}
        onMouseLeave={() => setHoverX(null)}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={pad.l + plotW} y1={sy(t)} y2={sy(t)} className="stroke-stone-100 dark:stroke-stone-800" />
            <text x={pad.l - 6} y={sy(t) + 3} textAnchor="end" className="fill-stone-500 text-[10px]">
              {+t.toFixed(3)}
            </text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} x={sx(t)} y={height - 6} textAnchor="middle" className="fill-stone-500 text-[10px]">
            {fmtMs(t)}
          </text>
        ))}
        <text x={pad.l} y={pad.t - 1} className="fill-stone-400 text-[10px]">
          {yLabel}
        </text>
        {refLine && (
          <g>
            <line x1={pad.l} x2={pad.l + plotW} y1={sy(refLine.y)} y2={sy(refLine.y)} className="stroke-red-400" strokeDasharray="4 3" />
            <text x={pad.l + plotW - 2} y={sy(refLine.y) - 3} textAnchor="end" className="fill-red-500 text-[10px]">
              {refLine.label}
            </text>
          </g>
        )}
        {series.map((s) => {
          let d = ''
          let pen = false
          for (const p of s.points) {
            if (p.y == null) {
              pen = false
              continue
            }
            d += `${pen ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`
            pen = true
          }
          return <path key={s.name} d={d} fill="none" stroke={s.color} strokeWidth={1.4} strokeLinejoin="round" />
        })}
        {hoverX != null && <line x1={sx(hoverX)} x2={sx(hoverX)} y1={pad.t} y2={pad.t + plotH} className="stroke-stone-400" strokeDasharray="3 3" />}
        <text x={pad.l + plotW} y={height - 6} textAnchor="end" className="fill-stone-400 text-[10px]" dy={-12}>
          {xLabel}
        </text>
      </svg>
      {hoverVals && (
        <div className="pointer-events-none absolute right-3 top-2 rounded border border-stone-200 bg-white/95 px-2 py-1 text-[11px] shadow-sm dark:border-stone-700 dark:bg-stone-900/95">
          {hoverVals.map(
            (v) =>
              v.p && (
                <div key={v.name} className="flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full" style={{ background: v.color }} />
                  {v.name}: <b>{v.p.y ?? '–'}</b> <span className="text-stone-400">@{v.p.x.toFixed(0)}ms</span>
                </div>
              ),
          )}
        </div>
      )}
    </div>
  )
}
