"""참고 저장소(camera-hal-analysis)의 고장 시나리오를 시뮬레이터 카탈로그로 가져온다.

원본: ../camera-hal/camera-hal-analysis/scripts/camera_hal_failure_data{,_v2}.py
출력: camhal_sim/data/reference_scenarios.json

시뮬레이터는 이 JSON만 읽으므로 참고 저장소 없이도 동작한다. 원본이 바뀌면
이 스크립트를 다시 실행한다. 순서(인덱스)가 결함 ID(F00..F36)의 기준이다.

    python3 scripts/import_reference.py [--src <camera-hal-analysis 경로>]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT.parent / "camera-hal" / "camera-hal-analysis"
OUT = ROOT / "camhal_sim" / "data" / "reference_scenarios.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    args = ap.parse_args()

    scripts = args.src / "scripts"
    if not (scripts / "camera_hal_failure_data.py").exists():
        print(f"참고 저장소를 찾을 수 없음: {scripts}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(scripts))
    from camera_hal_failure_data import CATEGORIES, FAILURE_TEMPLATES, PLATFORM_LINES
    from camera_hal_failure_data_v2 import FAILURE_TEMPLATES_V2

    templates = list(FAILURE_TEMPLATES) + list(FAILURE_TEMPLATES_V2)
    payload = {
        "source": "camera-hal-analysis/scripts/camera_hal_failure_data{,_v2}.py",
        "categories": CATEGORIES,
        "platforms": [asdict(p) for p in PLATFORM_LINES],
        "scenarios": [{"id": f"F{i:02d}", **asdict(t)} for i, t in enumerate(templates)],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(templates)} scenarios, {len(PLATFORM_LINES)} platforms -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
