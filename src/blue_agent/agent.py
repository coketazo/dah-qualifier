"""
blue_agent — 자율 방어 에이전트 (트랙 B), 실물 MAVLink 위에서 동작.

수동 MAVLink IDS: 표적이 브로드캐스트하는 텔레메트리를 수신·상관한다.
  - GPS_RAW_INT           : GPS 측정(스푸핑에 오염되는 값)
  - LOCAL_POSITION_NED    : 관성항법(INS) 독립 해 — GPS 비의존
  - HOME_POSITION         : NED 원점을 전지구 좌표로 앵커
  - EKF_STATUS_REPORT     : EKF 분산(스텔스 스푸핑에선 게이트 아래로 낮게 유지됨)
  - HEARTBEAT             : 모드/페일세이프

핵심 통찰: 스텔스 스푸핑은 '순간 EKF 혁신'을 작게 유지해 온보드 게이트를 통과한다.
그러나 GPS↔INS '누적' 발산은 계속 자란다 → policy.detect_gps_ins_divergence 로 잡는다.
탐지 시 능동 완화(제어평면): 표적에 EKF GPS-거부 레인 전환을 요청(가용성 보존).

브레인:
  - 규칙기반(항상): policy 교차정합 — 신뢰성 있는 탐지의 핵심
  - LLM 트리아지(선택, LLM_API_KEY): 탐지를 지휘관 보고문으로 정리(provider-agnostic)

실행:
  cd src && ../.venv/bin/python -m blue_agent.agent
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx

import mavproto
from mavproto.dialect import mavlink, ids
from mavproto.signing import derive_key
from mavproto.link import connect_agent
from common.geo import haversine_m, offset_m
from common.wire import Position, now_ts
from common import policy
from common.llm import make_client

HOST = os.environ.get("BLUE_HOST", "127.0.0.1")
MAV_PORT = int(os.environ.get("BLUE_MAV_PORT", "14550"))
CONTROL_URL = os.environ.get("BLUE_CONTROL_URL", "http://127.0.0.1:8137")
VERDICT_LOG = os.environ.get("VERDICT_LOG", "logs/verdicts.jsonl")
OPERATOR_PASSPHRASE = os.environ.get("MAV_OPER_KEY", "OPER-SECRET-2026")


def log(msg: str) -> None:
    print(f"[{now_ts()}] BLUE: {msg}", flush=True)


def write_verdict(rec: dict) -> None:
    from pathlib import Path
    p = Path(VERDICT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_ts(), **rec}, ensure_ascii=False) + "\n")


def llm_triage(detail: str, gps_ins_m: float) -> Optional[str]:
    """선택적 LLM 트리아지(provider-agnostic, LLM_API_KEY 있을 때만). 결정은 규칙이 이미 내림."""
    client = make_client()
    if client is None:
        return None
    try:
        r = client.complete([{"role": "user", "content":
            "너는 UAV 방어 관제 분석가다. 아래 탐지를 지휘관에게 한국어 2문장으로 보고하라"
            "(무엇이 왜 의심스러운가 + 권고). "
            f"탐지: {detail}, GPS-INS 누적발산 {gps_ins_m:.0f}m."}])
        return r.text or None
    except Exception as e:  # noqa: BLE001
        return f"(LLM 트리아지 실패: {e})"


def run(max_idle_s: float = 40.0) -> None:
    # 방어자는 공유키 보유(신뢰 노드). 하트비트로 서버에 등록 → 텔레메트리 스트림 수신.
    key = derive_key(OPERATOR_PASSPHRASE)
    conn = connect_agent(HOST, MAV_PORT, me=ids.blue, secret_key=key, sign_outgoing=True)
    conn.mav.heartbeat_send(mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    log(f"IDS 관찰 시작 mav={HOST}:{MAV_PORT} control={CONTROL_URL}")

    home: Optional[Position] = None
    latest_gps: Optional[Position] = None
    latest_ins: Optional[Position] = None
    peak_div = 0.0
    mitigated = False
    idle = 0.0

    while idle < max_idle_s and not mitigated:
        msg = conn.recv_match(blocking=True, timeout=0.3)
        if msg is None:
            idle += 0.3
            # 등록 유지용 주기 하트비트
            conn.mav.heartbeat_send(mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            continue
        idle = 0.0
        mt = msg.get_type()

        if mt == "HOME_POSITION":
            home = Position(lat=msg.latitude / 1e7, lon=msg.longitude / 1e7, alt_m=msg.altitude / 1000.0)
        elif mt == "GPS_RAW_INT":
            latest_gps = Position(lat=msg.lat / 1e7, lon=msg.lon / 1e7, alt_m=msg.alt / 1000.0)
        elif mt == "LOCAL_POSITION_NED" and home is not None:
            # INS 독립 해를 전지구 좌표로 복원(home + NED 변위)
            latest_ins = offset_m(home, msg.x, msg.y)

        # 교차정합: GPS(오염) ↔ INS(독립) 누적 발산
        if latest_gps is not None and latest_ins is not None:
            div = haversine_m(latest_gps, latest_ins)
            peak_div = max(peak_div, div)
            det = policy.detect_gps_ins_divergence(latest_gps, latest_ins)
            if det.verdict.value == "block":
                log(f"탐지! [{det.rule.value}] {det.reason.value} — {det.detail} "
                    f"(EKF 게이트는 통과했으나 누적 발산이 임계 초과)")
                note = llm_triage(det.detail, div)
                if note:
                    log(f"트리아지: {note.strip()}")
                write_verdict({"verdict": "block", "rule": det.rule.value, "reason": det.reason.value,
                               "gps_ins_divergence_m": round(div, 1), "detected_by": "blue_agent",
                               "detail": det.detail})
                # 능동 완화: 제어평면으로 EKF GPS-거부 레인 전환 요청(시스템은 안 죽인다).
                try:
                    httpx.post(f"{CONTROL_URL}/api/defense/mitigate",
                               params={"by": "blue_agent"}, timeout=3)
                    log("완화 발동 → 표적에 EKF 관성폴백 요청(가용성 보존).")
                    mitigated = True
                except Exception as e:  # noqa: BLE001
                    log(f"완화 요청 실패: {e}")

    log(f"관찰 종료. 최대 GPS-INS 발산={peak_div:.0f}m, 완화={'예' if mitigated else '아니오'}")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("종료(Ctrl-C).")
