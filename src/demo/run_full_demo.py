"""
레거시 에이전트 통합 데모 — mock_gcs + blue_agent + red_agent 를 각각 별도 프로세스로 띄워
실물 MAVLink 2.0 위에서 자율 red-vs-blue 공방을 재현하고 결과를 출력한다.

주의: 표적 v4 정본 검증은 demo.run_target_scenarios 다. 이 파일은 red/blue의 LLM-only
마이그레이션이 끝나기 전까지 예선 AI 증거로 사용하지 않는다.

토폴로지:
  mock_gcs(app)  : REST 제어평면 :{REST}  +  MAVLink 공격표면 udp:{MAV}
  red_agent      : udp:{MAV} 로 GPS_INPUT 스텔스 스푸핑
  blue_agent     : udp:{MAV} 텔레메트리 관측 → 교차정합 탐지 → REST 완화

실행:
  cd src && ../.venv/bin/python -m demo.run_full_demo
  # LLM_API_KEY(Groq 등 OpenAI 호환) 있으면 red 가 LLM 브레인 사용, 없으면 규칙기반.
  # SECURE=true 로 주면 표적이 서명강제(미서명 명령 거부) 모드로 뜬다.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

REST = int(os.environ.get("DEMO_REST_PORT", "8140"))
MAV = int(os.environ.get("DEMO_MAV_PORT", "14560"))
SECURE = os.environ.get("SECURE", "false")
BASE = f"http://127.0.0.1:{REST}"
SRC = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
LOG = "logs/events.jsonl"


def main() -> None:
    env = dict(os.environ, SECURE=SECURE, LOG_PATH=LOG,
               MAV_HOST="127.0.0.1", MAV_PORT=str(MAV),
               RED_HOST="127.0.0.1", RED_MAV_PORT=str(MAV), RED_LOG="logs/red_agent.log",
               RED_CONTROL_URL=BASE,
               BLUE_HOST="127.0.0.1", BLUE_MAV_PORT=str(MAV), BLUE_CONTROL_URL=BASE,
               VERDICT_LOG="logs/verdicts.jsonl")
    (SRC / "logs").mkdir(exist_ok=True)
    for f in ("events.jsonl", "verdicts.jsonl", "red_agent.log"):
        (SRC / "logs" / f).unlink(missing_ok=True)

    procs: list[subprocess.Popen] = []

    def spawn(args):
        p = subprocess.Popen(args, cwd=SRC, env=env)
        procs.append(p)
        return p

    try:
        spawn([PYTHON, "-m", "uvicorn", "mock_gcs.app:app", "--port", str(REST),
               "--log-level", "warning"])
        for _ in range(50):
            try:
                httpx.get(f"{BASE}/api/status", timeout=1); break
            except Exception:
                time.sleep(0.2)
        st = httpx.get(f"{BASE}/api/status", timeout=2).json()
        print(f"[demo] mock_gcs up · REST {BASE} · MAVLink {st['mav_endpoint']} · SECURE={SECURE}")

        spawn([PYTHON, "-m", "blue_agent.agent"])
        time.sleep(1.2)
        print("[demo] blue_agent IDS 관찰 시작. red_agent 자율 공격 개시…\n")

        subprocess.run([PYTHON, "-m", "red_agent.agent"], cwd=SRC, env=env, timeout=120)

        time.sleep(2.5)
        t = httpx.get(f"{BASE}/api/truth", timeout=3).json()
        print("\n================ 결과 ================")
        print(f"  MODE        = {t['mode']}   FAILSAFE = {t['failsafe']}")
        print(f"  EKF 수평분산 = {t['ekf_pos_horiz_var']}  (게이트 1.0 아래면 온보드 미탐)")
        print(f"  추정-실제 편이 = {t['estimate_true_bias_m']} m")
        print(f"  미서명 명령 거부 = {t['rejected_unsigned']}")
        print(f"  DEFENDED    = {t['defended']} ({t['defended_by']})")
        print(f"  MISSION LOSS = {t['mission_compromised']}")
        print(f"  PLATFORM AVAIL = {t['platform_availability']}")
        print(f"  C2 AVAIL       = {t['c2_availability']}")
        print(f"  대시보드: {BASE}/  · 로그: src/{LOG}, src/logs/verdicts.jsonl, src/logs/red_agent.log")
        print("=====================================")
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
