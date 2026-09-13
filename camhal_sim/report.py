"""결과 출력 — 텍스트 요약, JSON, 단일 파일 HTML 타임라인, Jira 이슈 레코드.

Jira 레코드는 camera-hal-analysis 의 raw_issues.json 스키마(summary/description/
status/priority/labels/components)와 본문 마커(*대상 플랫폼*/*HAL 버전*/*고장 분류*/
*심각도*)를 따른다 — 시뮬레이션에서 재현한 고장을 분석 도구의 미해결 이슈로 넣어
유사 해결 사례 추천을 시험할 수 있다.
"""
from __future__ import annotations

import html
import json
from collections import Counter

from .model import MS
from .runner import RunResult, SweepRow

SEV_PRIORITY = {"Blocker": "Highest", "Critical": "High", "Major": "Medium", "Minor": "Low"}
LANES = ("open_result", "configure", "shutter", "result", "partial_result", "notify_error", "frame_drop",
         "flush_returned", "close_returned", "sensor_streaming", "camera_in_use", "device_status",
         "torch_status", "request_timeout", "stream_stall", "provider_memory", "provider_died",
         "ipc_call", "diag_access", "hw_usb")


# --------------------------------------------------------------------------- #
# 텍스트
# --------------------------------------------------------------------------- #
def render_text(r: RunResult, log_lines: int = 25) -> str:
    out = []
    title = r.fault.title(r.platform.code) if r.fault else f"{r.workload.title}"
    out.append(f"== {title}")
    out.append(f"   platform={r.platform.code}  workload={r.workload.name}  seed={r.seed}  "
               f"defects={','.join(r.defects) or '(none)'}")
    out.append(f"   sim={r.sim_ms / 1000:.2f}s  wall={r.wall_ms:.0f}ms  events={len(r.events)}  "
               f"results={r.counters.get('result', 0)}  drops={r.counters.get('frame_drop', 0)}")
    if r.fault:
        missing = set(r.fault.expect) - r.codes
        if r.defects:
            verdict = "DETECTED" if not missing else f"MISSED {sorted(missing)}"
            out.append(f"   expected={','.join(r.fault.expect)} -> {verdict}")
    if not r.violations:
        out.append("   위반 없음")
    for v in r.violations:
        out.append(f"   [{v.severity:<8}] {v.code:<32} ({v.category}) x{v.count}")
        out.append(f"              {v.title}: {v.detail}")
    if log_lines:
        important = [l for l in r.logs if l.level in ("W", "E", "F")]
        label = "W/E/F"
        if not important:
            important, label = r.logs, "전체"
        out.append(f"   -- logcat ({label} {len(important)}건 중 {min(log_lines, len(important))}건)")
        out += ["   " + l.render() for l in important[:log_lines]]
    return "\n".join(out)


def render_sweep_text(rows: list[SweepRow]) -> str:
    out = [f"{'ID':<4} {'category':<18} {'platform':<18} {'workload':<25} {'faulty':<8} {'fixed':<6} detected codes"]
    for row in rows:
        f = row.fault
        out.append(f"{f.id:<4} {f.category:<18} {f.platform:<18} {f.workload:<25} "
                   f"{'DETECT' if row.detected else 'MISS':<8} {'CLEAN' if row.clean else 'DIRTY':<6} "
                   f"{','.join(sorted(row.faulty.codes))}"
                   + (f"  | fixed: {','.join(sorted(row.fixed.codes))}" if not row.clean else ""))
    ok = sum(r.ok for r in rows)
    out.append(f"\n{ok}/{len(rows)} 결함이 주입 시 검출되고 수정 시 위반 0건")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def to_json(r: RunResult, include_events: bool = True) -> dict:
    d = r.summary()
    if r.fault:
        d["reference"] = {"id": r.fault.id, "summary": r.fault.title(r.platform.code),
                          "category": r.fault.category, "expect": list(r.fault.expect),
                          "root_cause": r.fault.root_cause, "resolution": r.fault.resolution}
    if include_events:
        d["events"] = [e.to_dict() for e in r.events]
        d["logs"] = [l.render() for l in r.logs]
    return d


def sweep_to_json(rows: list[SweepRow]) -> dict:
    return {"total": len(rows), "ok": sum(r.ok for r in rows), "rows": [
        {"id": r.fault.id, "category": r.fault.category, "platform": r.fault.platform,
         "workload": r.fault.workload, "defect": r.fault.defect, "expect": list(r.fault.expect),
         "faulty_codes": sorted(r.faulty.codes), "fixed_codes": sorted(r.fixed.codes),
         "detected": r.detected, "clean": r.clean} for r in rows]}


# --------------------------------------------------------------------------- #
# Jira 이슈 (camera-hal-analysis raw_issues 스키마)
# --------------------------------------------------------------------------- #
def to_jira_issue(r: RunResult, key: str = "") -> dict:
    top = r.violations[0] if r.violations else None
    category = (r.fault.category if r.fault else "") or (top.category if top else "Performance")
    severity = (r.fault.severity if r.fault else "") or (top.severity if top else "Minor")
    headline = top.title if top else r.workload.title
    summary = f"[{r.platform.code}] {r.workload.title} 중 {headline}"
    symptom = "\n".join(f"* {v.title} — {v.detail}" for v in r.violations) or "재현 시 위반 없음"
    repro = "\n".join(f"# {s}" for s in r.workload.steps)
    logs = "\n".join(l.render() for l in [l for l in r.logs if l.level in ("W", "E", "F")][:12])
    hal = f"{r.platform.fw_prefix}.sim.{r.seed}"
    description = f"""h2. 고객 고장 보고

*고객사*: Simulation Lab
*보고자*: camhal-sim
*대상 플랫폼*: {r.platform.code}
*HAL 버전*: {hal}
*호스트/OS*: {r.platform.host_platforms[0] if r.platform.host_platforms else '-'}
*카메라 구성*: 시뮬레이션 ({r.workload.name})
*발견일*: -
*고장 분류*: {category}
*심각도*: {severity}

h2. 증상 (Symptom)

{symptom}

h2. 재현 절차 (Reproduction)

{repro}

h2. 로그 발췌 (Log Excerpt)

{{code}}
{logs}
{{code}}

h2. 영향 (Impact)

해당 고장은 *{severity}* 등급으로 분류됩니다. 시뮬레이터 seed={r.seed} 로 결정론적으로 재현됩니다.
"""
    return {
        "key": key or f"SIM-{r.platform.fw_prefix}-{r.workload.name}",
        "summary": summary,
        "description": description,
        "status": "Open",
        "priority": SEV_PRIORITY.get(severity, "Medium"),
        "labels": [category.replace(" ", "-"), r.platform.family.replace(" ", "-"), "simulation",
                   f"hal-{r.platform.fw_prefix}"],
        "components": [r.platform.family],
        "comments": [],
        "sim": {"defects": list(r.defects), "violations": [v.code for v in r.violations], "seed": r.seed},
    }


# --------------------------------------------------------------------------- #
# HTML 타임라인
# --------------------------------------------------------------------------- #
_CSS = """
:root{--bg:#fbfbf9;--fg:#1d1f24;--muted:#6a6f78;--line:#e3e3de;--card:#fff;--accent:#2f6fdf;
--bad:#c4372c;--warn:#b7791f;--ok:#2d8a4e}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15171b;--fg:#e6e6e3;--muted:#9aa0a8;
--line:#2c3038;--card:#1c1f24;--accent:#6c9cf0;--bad:#ef6b5f;--warn:#e0a94a;--ok:#5cc184}}
:root[data-theme=dark]{--bg:#15171b;--fg:#e6e6e3;--muted:#9aa0a8;--line:#2c3038;--card:#1c1f24;
--accent:#6c9cf0;--bad:#ef6b5f;--warn:#e0a94a;--ok:#5cc184}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:1100px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
.meta{color:var(--muted);font-size:13px}.card{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;margin:8px 0}.sev{font-weight:600;font-size:12px;padding:1px 6px;
border-radius:4px;border:1px solid currentColor}.Blocker,.Critical{color:var(--bad)}.Major{color:var(--warn)}
.Minor{color:var(--muted)}code,pre{font:12px/1.45 ui-monospace,Menlo,monospace}pre{white-space:pre-wrap;
margin:0}.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.ok{color:var(--ok)}.bad{color:var(--bad)}svg text{fill:var(--muted);font:11px system-ui,sans-serif}
details summary{cursor:pointer;color:var(--accent)}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;font-size:13px;vertical-align:top}
"""


def _timeline_svg(r: RunResult) -> str:
    kinds = [k for k in LANES if k in r.counters] + sorted(k for k in r.counters if k not in LANES)
    if not r.events:
        return ""
    t_end = max(e.t_ns for e in r.events) or 1
    left, width, lane_h = 150, 900, 18
    bad_kinds = {"notify_error", "frame_drop", "request_timeout", "stream_stall", "provider_died"}
    rows = []
    for i, k in enumerate(kinds):
        y = 10 + i * lane_h
        rows.append(f'<text x="4" y="{y + 12}">{html.escape(k)} ({r.counters[k]})</text>'
                    f'<line x1="{left}" x2="{left + width}" y1="{y + 9}" y2="{y + 9}" stroke="var(--line)"/>')
        evs = [e for e in r.events if e.kind == k]
        step = max(1, len(evs) // 900)
        color = "var(--bad)" if k in bad_kinds else "var(--accent)"
        for e in evs[::step]:
            x = left + width * e.t_ns / t_end
            rows.append(f'<rect x="{x:.1f}" y="{y + 3}" width="1.6" height="12" fill="{color}"/>')
    h = 20 + len(kinds) * lane_h
    ticks = "".join(f'<text x="{left + width * f:.0f}" y="{h + 4}">{t_end * f / 1e9:.1f}s</text>'
                    for f in (0, .25, .5, .75, 1))
    return (f'<svg viewBox="0 0 {left + width + 30} {h + 14}" width="{left + width + 30}" '
            f'role="img" aria-label="event timeline">{"".join(rows)}{ticks}</svg>')


def render_html(r: RunResult) -> str:
    esc = html.escape
    title = r.fault.title(r.platform.code) if r.fault else r.workload.title
    viol = "".join(
        f'<div class="card"><span class="sev {v.severity}">{v.severity}</span> <b>{esc(v.code)}</b> '
        f'<span class="meta">· {esc(v.category)} · x{v.count}</span><div>{esc(v.title)}</div>'
        f'<div class="meta">{esc(v.detail)}</div><details><summary>근거 사건</summary>'
        f'<pre>{esc(json.dumps(v.evidence, ensure_ascii=False, indent=1)[:4000])}</pre></details></div>'
        for v in r.violations) or '<div class="card ok">위반 없음</div>'
    ref = ""
    if r.fault:
        missing = set(r.fault.expect) - r.codes
        verdict = ('<span class="ok">기대 위반 모두 검출</span>' if not missing
                   else f'<span class="bad">미검출: {esc(", ".join(sorted(missing)))}</span>') if r.defects else \
            '<span class="ok">결함 미주입(수정 상태) 실행</span>'
        ref = f"""<h2>참고 시나리오 {r.fault.id}</h2><div class="card"><table>
<tr><th>분류/심각도</th><td>{esc(r.fault.category)} / {esc(r.fault.severity)}</td></tr>
<tr><th>주입 결함</th><td><code>{esc(r.fault.defect)}</code> — {verdict}</td></tr>
<tr><th>증상</th><td>{esc(r.fault.symptom)}</td></tr>
<tr><th>근본 원인</th><td>{esc(r.fault.root_cause)}</td></tr>
<tr><th>해결책</th><td>{esc(r.fault.resolution)}</td></tr>
<tr><th>참고 로그</th><td><pre>{esc(r.fault.log_excerpt)}</pre></td></tr></table></div>"""
    steps = "".join(f"<li>{esc(s)}</li>" for s in r.workload.steps)
    logs = "\n".join(esc(l.render()) for l in r.logs[:600])
    kinds = Counter(l.level for l in r.logs)
    return f"""<title>camhal-sim · {esc(r.platform.code)} · {esc(r.workload.name)}</title>
<style>{_CSS}</style><main>
<h1>{esc(title)}</h1>
<div class="meta">platform {esc(r.platform.code)} · workload {esc(r.workload.name)} · seed {r.seed} ·
defects {esc(", ".join(r.defects) or "none")} · sim {r.sim_ms / 1000:.2f}s · events {len(r.events)}</div>
<h2>판정</h2>{viol}{ref}
<h2>재현 절차</h2><ol>{steps}</ol>
<h2>사건 타임라인</h2><div class="scroll">{_timeline_svg(r)}</div>
<h2>logcat ({len(r.logs)}줄 · W {kinds.get('W', 0)} · E {kinds.get('E', 0)} · F {kinds.get('F', 0)})</h2>
<div class="scroll"><pre style="padding:10px">{logs}</pre></div></main>"""
