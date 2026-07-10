"""
LLM 에이전트 실전 교전 하네스 — red(LLM) 가 살아있는 표적을 공격하고 blue(LLM/규칙)가
방어한다. 관측→툴선택→적응→결과 trace 를 수집한다(보고서 §6 AI 에이전트 증거 생성).

이것은 표적 수용시험(demo.run_target_scenarios)과 **다르다**:
  - run_target_scenarios : 결정론적 하네스가 공방을 재현 = 표적 신뢰경계 검증. 에이전트 아님.
  - run_agent_engagement : LLM red 가 스스로 정찰→서명프로브→적응→스푸핑을 결정 = AI 증거.

red 는 LLM-only 이므로 이 하네스는 LLM_API_KEY 가 필요하다. 없으면 명확히 안내하고 종료한다.
키 없는 재현은 표적 수용시험을 쓰라.

실행:
  cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m demo.run_agent_engagement
  cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m demo.run_agent_engagement --secure   # signed C2 → red 가 물리 RF 로 적응
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from common.llm import make_client

SRC = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _wait_rest(base: str, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            httpx.get(f"{base}/api/status", timeout=1).raise_for_status()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"target REST startup timeout: {base}")


def _tail_jsonl(path: Path, n: int = 40) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()[-n:]


def engage(*, secure: bool, with_blue: bool = True, degrade_extnav_sigma: Optional[float] = None,
           rest_port: int = 8137, c2_port: int = 14550, sensor_port: int = 14600) -> dict:
    base = f"http://127.0.0.1:{rest_port}"
    tag = f"engagement_{'secure' if secure else 'insecure'}"
    if not with_blue:
        tag += "_noblue"
    if degrade_extnav_sigma:
        tag += "_extnavlow"
    logs = SRC / "logs"
    logs.mkdir(exist_ok=True)
    red_trace = logs / f"{tag}_red_trace.jsonl"
    blue_verdicts = logs / f"{tag}_blue_verdicts.jsonl"
    for f in (red_trace, blue_verdicts, logs / f"{tag}_events.jsonl"):
        f.unlink(missing_ok=True)

    env = dict(os.environ, SECURE=str(secure).lower(),
               LOG_PATH=f"logs/{tag}_events.jsonl",
               MAV_HOST="127.0.0.1", MAV_PORT=str(c2_port),
               SENSOR_HOST="127.0.0.1", SENSOR_MAV_PORT=str(sensor_port),
               RED_HOST="127.0.0.1", RED_MAV_PORT=str(c2_port),
               RED_SENSOR_HOST="127.0.0.1", RED_SENSOR_PORT=str(sensor_port),
               RED_CONTROL_URL=base, RED_TRACE=str(red_trace),
               BLUE_HOST="127.0.0.1", BLUE_MAV_PORT=str(c2_port),
               BLUE_CONTROL_URL=base, VERDICT_LOG=str(blue_verdicts))

    procs: list[subprocess.Popen] = []

    def spawn(args):
        p = subprocess.Popen(args, cwd=SRC, env=env)
        procs.append(p)
        return p

    try:
        spawn([PYTHON, "-m", "uvicorn", "mock_gcs.app:app", "--port", str(rest_port),
               "--log-level", "warning"])
        _wait_rest(base)
        if degrade_extnav_sigma:
            # 독립 ExternalNav(VIO) 품질 저하 환경 → blue 는 ExternalNav RTL 대신 safe_hold 선택.
            httpx.post(f"{base}/api/_env/degrade_extnav",
                       params={"sigma_m": degrade_extnav_sigma, "source": "engagement_harness"}, timeout=3)
        if with_blue:
            spawn([PYTHON, "-m", "blue_agent.agent"])
            time.sleep(1.2)

        # LLM red 가 스스로 교전을 주도한다(관측→적응→스푸핑). 이 프로세스가 red 를 구동.
        red = spawn([PYTHON, "-m", "red_agent.agent", "--max-steps", "24"])
        red.wait(timeout=180)
        time.sleep(2.0)
        truth = httpx.get(f"{base}/api/truth", timeout=3).json()
        return {"truth": truth,
                "red_trace": _tail_jsonl(red_trace),
                "blue_verdicts": _tail_jsonl(blue_verdicts)}
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        time.sleep(0.4)


def _red_channels(red_trace: list[str]) -> list[str]:
    """red trace 에서 실제로 시도한 공격 채널 순서를 추출."""
    import json
    seq = []
    for line in red_trace:
        e = json.loads(line)
        if e.get("event") == "step" and e.get("action") in ("c2_gps_inject", "rf_gnss_spoof"):
            if not seq or seq[-1] != e["action"]:
                seq.append(e["action"])
    return seq


def _print_engagement(title: str, r: dict) -> None:
    import json
    t = r["truth"]
    print(f"\n### {title}")
    print("────────── red 관측→적응 trace (요약) ──────────")
    for line in r["red_trace"]:
        e = json.loads(line)
        if e.get("event") == "step":
            obs = e["observation"]
            key = obs.get("assessment") or obs.get("inferred_signing_enforced")
            print(f"  [{e['i']:>2}] {e['action']:<15} reason={str(e['args'].get('reason',''))[:56]}")
            if key is not None:
                print(f"       └ {str(key)[:80]}")
        elif e.get("event") == "end":
            print(f"  END success={e.get('success')} :: {str(e.get('summary'))[:90]}")
    print("────────── blue 대응선택 trace ──────────")
    for line in r["blue_verdicts"]:
        e = json.loads(line)
        d = e.get("decision", {})
        print(f"  response={d.get('response')} by={d.get('by')} :: {d.get('rationale')}")
    print("────────── 최종 표적 상태 ──────────")
    print(f"  mission_compromised={t['mission_compromised']} defended={t['defended']} "
          f"bias={t['estimate_true_bias_m']}m nav={t['nav_source']} defense_state={t['defense_state']}\n"
          f"  c2_reject={t['rejected_unsigned']} sensor_reject={t['rejected_c2_sensor']} "
          f"extnav_sigma={t.get('external_nav_sigma_m')}m  ·  red 채널: {' → '.join(_red_channels(r['red_trace'])) or '없음'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 에이전트 실전 교전 하네스")
    ap.add_argument("--secure", action="store_true", help="signed C2 배치")
    ap.add_argument("--no-blue", action="store_true", help="blue 없이 red 의 원 공격 경로만 관측")
    ap.add_argument("--degrade-extnav", type=float, metavar="SIGMA_M",
                    help="독립 ExternalNav 품질 저하(sigma_m) → blue 가 safe_hold 선택하는지 관측")
    ap.add_argument("--compare", action="store_true",
                    help="insecure vs secure 를 연달아 돌려 red 의 채널 적응 대조(blue 없이)")
    args = ap.parse_args()

    if make_client() is None:
        print("이 하네스는 LLM_API_KEY 가 필요하다 (red_agent 는 LLM-only).\n"
              "  예: LLM_API_KEY=gsk_... python -m demo.run_agent_engagement --compare\n"
              "키 없는 재현은 표적 수용시험을 쓰라: python -m demo.run_target_scenarios",
              file=sys.stderr)
        return 2

    if args.compare:
        # 같은 red 가 C2 서명 정책에 따라 어떤 채널을 쓰는지 대조(적응의 직접 증거).
        print("\n════════ 적응 대조: 같은 LLM red, C2 정책만 다름 (blue 없음) ════════")
        ins = engage(secure=False, with_blue=False, rest_port=8151, c2_port=14651, sensor_port=14751)
        _print_engagement("A. insecure C2 (미서명 수용)", ins)
        sec = engage(secure=True, with_blue=False, rest_port=8152, c2_port=14652, sensor_port=14752)
        _print_engagement("B. secure C2 (서명강제)", sec)
        print("\n────────── 대조 요약 ──────────")
        print(f"  insecure red 채널: {' → '.join(_red_channels(ins['red_trace'])) or '없음'}")
        print(f"  secure   red 채널: {' → '.join(_red_channels(sec['red_trace'])) or '없음'}")
        print("  → 서명강제(B)에서 red 가 C2 주입 무효를 관측하고 물리 GNSS RF 로 적응하면 적응 성립.")
        return 0

    print(f"\n### LLM 에이전트 교전 (secure={args.secure}, blue={not args.no_blue}, "
          f"extnav_저하={args.degrade_extnav}) …")
    r = engage(secure=args.secure, with_blue=not args.no_blue,
               degrade_extnav_sigma=args.degrade_extnav)
    _print_engagement("교전 결과", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
