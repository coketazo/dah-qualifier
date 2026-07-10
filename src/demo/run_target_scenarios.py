"""표적/방어 수용시험 — 에이전트 지능과 분리한 신뢰경계 검증.

이 러너는 red 에이전트의 성과를 주장하지 않는다. 시나리오 하네스가
  1) C2 링크 열화(RF 환경효과),
  2) 별도 GNSS 센서 모사 포트의 점진 위치 편이
를 재현해 mock 임무시스템과 방어 경계가 설계대로 동작하는지만 검증한다.

T1 무방어           : 물리 GNSS 모사 → 임무 무결성 상실.
T2 Blue 방어        : ExternalNav 교차정합 → GNSS 격리/복구.
T3 Signed C2 + Blue : 미서명 C2 명령/GPS_INPUT은 거부되지만, C2 밖 물리 센서
                      오염은 별도 위협이므로 Blue가 다시 탐지/복구.

실행: cd src && ../.venv/bin/python -m demo.run_target_scenarios
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

import mavproto
from mavproto.dialect import mavlink, ids
from mavproto.link import connect_agent
from common.geo import offset_m
from common.wire import Position

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


def _send_gps_input(conn, p: Position) -> None:
    ignore = (mavlink.GPS_INPUT_IGNORE_FLAG_VEL_HORIZ |
              mavlink.GPS_INPUT_IGNORE_FLAG_VEL_VERT |
              mavlink.GPS_INPUT_IGNORE_FLAG_SPEED_ACCURACY |
              mavlink.GPS_INPUT_IGNORE_FLAG_HORIZONTAL_ACCURACY |
              mavlink.GPS_INPUT_IGNORE_FLAG_VERTICAL_ACCURACY)
    conn.mav.gps_input_send(
        int(time.time() * 1e6), 0, ignore, 0, 0,
        mavlink.GPS_FIX_TYPE_3D_FIX, int(p.lat * 1e7), int(p.lon * 1e7), p.alt_m,
        0.7, 0.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 12, 0)


def _exercise_unsigned_c2(c2_port: int, base: str) -> None:
    """signed C2에서 명령과 센서 주입이 모두 거부되는지 확인."""
    conn = connect_agent("127.0.0.1", c2_port, me=ids.red, secret_key=None)
    conn.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    conn.mav.command_long_send(ids.vehicle.system, ids.vehicle.component,
                               mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
                               0, 0, 0, 0, 0, 0, 0)
    t = httpx.get(f"{base}/api/truth", timeout=2).json()
    _send_gps_input(conn, Position(**t["ekf_position"]))
    time.sleep(0.6)
    conn.close()


def _run(name: str, *, secure: bool, with_blue: bool,
         rest_port: int, c2_port: int, sensor_port: int,
         probe_secure_c2: bool = False) -> dict:
    base = f"http://127.0.0.1:{rest_port}"
    tag = name.replace(" ", "_")
    env = dict(os.environ, SECURE=str(secure).lower(),
               LOG_PATH=f"logs/{tag}_events.jsonl",
               MAV_HOST="127.0.0.1", MAV_PORT=str(c2_port),
               SENSOR_HOST="127.0.0.1", SENSOR_MAV_PORT=str(sensor_port),
               BLUE_HOST="127.0.0.1", BLUE_MAV_PORT=str(c2_port),
               BLUE_CONTROL_URL=base, VERDICT_LOG=f"logs/{tag}_verdicts.jsonl",
               LLM_PROVIDER="none")
    (SRC / "logs").mkdir(exist_ok=True)
    for f in (SRC / "logs" / f"{tag}_events.jsonl",
              SRC / "logs" / f"{tag}_verdicts.jsonl"):
        f.unlink(missing_ok=True)

    procs: list[subprocess.Popen] = []

    def spawn(args):
        p = subprocess.Popen(args, cwd=SRC, env=env)
        procs.append(p)
        return p

    try:
        spawn([PYTHON, "-m", "uvicorn", "mock_gcs.app:app", "--port", str(rest_port),
               "--log-level", "warning"])
        _wait_rest(base)
        if with_blue:
            spawn([PYTHON, "-m", "blue_agent.agent"])
            time.sleep(1.2)

        if probe_secure_c2:
            _exercise_unsigned_c2(c2_port, base)

        # RF 링크 환경효과. 공격 API가 아니라 시뮬레이터 하네스 제어다.
        httpx.post(f"{base}/api/_env/link_degrade",
                   params={"quality": 0.1, "hold_s": 10,
                           "source": "target_acceptance_harness"}, timeout=3)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if httpx.get(f"{base}/api/truth", timeout=2).json()["mode"] == "RTL":
                break
            time.sleep(0.2)

        # C2와 분리된 localhost 센서 모사 포트: 물리 GNSS RF 공격의 어댑터.
        sensor = connect_agent("127.0.0.1", sensor_port,
                               me=ids.sensor_emulator, secret_key=None)
        last_inj: Position | None = None
        for _ in range(45):
            t = httpx.get(f"{base}/api/truth", timeout=2).json()
            base_pos = Position(**t["ekf_position"])
            last_inj = offset_m(base_pos, 0.0, -6.0)   # 현재 추정 6m 서편으로 점진 편이
            _send_gps_input(sensor, last_inj)
            time.sleep(0.35)
        # settle 창: 새 편이를 더하지 않고 마지막 주입 위치를 유지(freeze)한다. 주입이 끊기면
        # ext_gps 가 실제 GNSS 로 복귀해 마지막 프레임에만 큰 순간 혁신이 드러나므로,
        # slow-takeover 가 '진행 중'인 정직한 최종 스냅샷(작은 혁신·spoof 유지)을 만든다.
        for _ in range(8):
            if last_inj is not None:
                _send_gps_input(sensor, last_inj)
            time.sleep(0.3)
        final = httpx.get(f"{base}/api/truth", timeout=3).json()
        sensor.close()
        return final
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        time.sleep(0.4)


def main() -> None:
    cases = [
        ("T1 무방어", dict(secure=False, with_blue=False,
                         rest_port=8161, c2_port=14661, sensor_port=14761)),
        ("T2 Blue방어", dict(secure=False, with_blue=True,
                            rest_port=8162, c2_port=14662, sensor_port=14762)),
        ("T3 SignedC2+Blue", dict(secure=True, with_blue=True,
                                 rest_port=8163, c2_port=14663, sensor_port=14763,
                                 probe_secure_c2=True)),
    ]
    rows: list[tuple[str, dict]] = []
    for name, cfg in cases:
        print(f"\n### {name} 표적 수용시험 …")
        t = _run(name, **cfg)
        rows.append((name, t))
        print(f"    compromised={t['mission_compromised']} defended={t['defended']} "
              f"bias={t['estimate_true_bias_m']}m c2_reject={t['rejected_unsigned']} "
              f"sensor_reject={t['rejected_c2_sensor']} nav={t['nav_source']}")

    a, b, c = (t for _, t in rows)
    assert a["mission_compromised"] and not a["defended"], a
    assert not b["mission_compromised"] and b["defended"], b
    assert not c["mission_compromised"] and c["defended"], c
    assert c["rejected_unsigned"] >= 2 and c["rejected_c2_sensor"] >= 1, c

    print("\n================ 표적 신뢰경계 수용 결과 ================")
    print(f"{'시나리오':<19}{'MISSION_LOSS':<14}{'DEFENDED':<11}{'편이(m)':<9}"
          f"{'C2거부':<8}{'센서주입거부':<12}{'NAV'}")
    print("-" * 82)
    for name, t in rows:
        print(f"{name:<19}{str(t['mission_compromised']):<14}{str(t['defended']):<11}"
              f"{str(t['estimate_true_bias_m']):<9}{str(t['rejected_unsigned']):<8}"
              f"{str(t['rejected_c2_sensor']):<12}{t['nav_source']}")
    print("=" * 82)
    print("수용기준 통과: C2 인증과 물리 센서 신뢰를 분리했고, Blue는 C2 서명으로 막을 수 없는 "
          "센서 오염을 독립 ExternalNav 교차정합으로 완화했다.")


if __name__ == "__main__":
    main()
