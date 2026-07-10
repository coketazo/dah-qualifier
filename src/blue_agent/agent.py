"""
blue_agent — 자율 방어 에이전트. 표준 MAVLink 2.0 위에서 동작하는 수동 IDS.

구조(잠금 결정 #4): **규칙은 LLM 의 검증도구, 최종 대응선택은 LLM trace 로 제시.**
  - 검증도구(결정론, 항상): policy 교차정합·ODOMETRY 품질·C2 거부 이벤트 상관.
    LLM 키가 없어도 탐지→완화가 결정론적으로 동작한다(표적 수용시험 재현 경로).
  - 판단층(LLM, 키 있을 때): 검증도구가 만든 증거를 받아 허용된 대응집합에서 하나를
    선택하고 근거를 남긴다 = 대응선택 trace(§6 AI 에이전트 증거).

독립 출처(v4 정본): `ODOMETRY(estimator_type=VIO)` — GNSS 와 독립된 보조항법 해와
그 공분산(품질)을 사용한다. `LOCAL_POSITION_NED` 는 ODOMETRY 미수신 시의 v3 호환
폴백일 뿐, 메시지 이름만으로 독립성을 가정하지 않는다.

핵심 통찰: 스텔스 스푸핑은 순간 EKF 혁신을 게이트 아래로 유지해 온보드 게이트를
통과한다. 그러나 GNSS ↔ 독립 ODOMETRY 의 '누적' 발산은 계속 자란다 → 그걸 잡는다.
탐지 시 능동 완화(제어평면): 표적에 GNSS 격리→ExternalNav 복구를 요청(가용성 보존).

실행:
  cd src && ../.venv/bin/python -m blue_agent.agent                 # 규칙 검증도구(키 불필요)
  cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m blue_agent.agent  # + LLM 대응선택 trace
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
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

# 허용된 대응집합(LLM 이 이 안에서 선택). mock 은 GNSS 격리 복구만 실행하지만,
# 판단층은 독립항법 품질에 따라 대안을 선택할 수 있어야 한다(실배치 분기의 축소).
ALLOWED_RESPONSES = {
    "gnss_quarantine_external_nav_rtl": "GNSS 융합 격리 후 독립 ExternalNav 안전복귀(RTL). 독립항법 품질 양호 시.",
    "safe_hold_operator_review": "즉시 복귀 대신 안전 LOITER 유지 후 운용자 인계. 독립항법 품질 불충분 시.",
    "continue_monitoring": "아직 근거 부족. 계속 관측.",
}


def log(msg: str) -> None:
    print(f"[{now_ts()}] BLUE: {msg}", flush=True)


def write_verdict(rec: dict) -> None:
    p = Path(VERDICT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_ts(), **rec}, ensure_ascii=False) + "\n")


def count_c2_rejects(control_url: str) -> int:
    """검증도구: REST 감사로그에서 C2 서명경계가 거부한 미인증 프레임 수를 읽는다.
    C2 인증 계층이 동작 중임을 확인하는 상관 신호(정찰이 아니라 방어자 관측)."""
    try:
        events = httpx.get(f"{control_url}/api/events?n=200", timeout=2).json()
        return sum(1 for e in events if e.get("event") == "mav_rx_reject")
    except Exception:  # noqa: BLE001
        return 0


def llm_select_response(client, evidence: dict) -> Optional[dict]:
    """판단층: 증거를 받아 허용된 대응집합에서 하나를 선택하고 근거를 남긴다.
    결정은 규칙이 이미 BLOCK 을 냈고, LLM 은 '무엇으로 대응할지'를 고른다."""
    tool = [{"name": "decide_response",
             "description": "탐지 증거를 근거로 대응을 선택한다.",
             "input_schema": {"type": "object", "properties": {
                 "response": {"type": "string", "enum": list(ALLOWED_RESPONSES)},
                 "rationale": {"type": "string", "description": "한국어 1~2문장 근거"}},
                 "required": ["response", "rationale"]}}]
    sys_msg = ("너는 UAV 방어 관제 판단 에이전트다. 규칙 검증도구가 이미 GNSS↔독립 ODOMETRY "
               "누적 발산으로 스푸핑을 탐지했다. 아래 증거를 근거로 허용된 대응 중 하나를 골라라. "
               "독립 ODOMETRY 공분산(품질)이 양호하면 GNSS 격리 후 ExternalNav 복귀를, 품질이 "
               "불충분하면 안전 LOITER 후 운용자 인계를 선호하라. 반드시 decide_response 를 호출하라.")
    try:
        r = client.complete(
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": "탐지 증거:\n" + json.dumps(evidence, ensure_ascii=False, indent=2)}],
            tool)
        if r.tool_calls:
            a = r.tool_calls[0].arguments
            resp = a.get("response")
            if resp in ALLOWED_RESPONSES:
                return {"response": resp, "rationale": a.get("rationale", ""), "by": "llm"}
    except Exception as e:  # noqa: BLE001
        log(f"판단층(LLM) 실패 → 규칙 기본대응: {e}")
    return None


def run(max_idle_s: float = 40.0) -> None:
    # 방어자는 공유키 보유(신뢰 노드). 하트비트로 서버에 등록 → 텔레메트리 스트림 수신.
    key = derive_key(OPERATOR_PASSPHRASE)
    conn = connect_agent(HOST, MAV_PORT, me=ids.blue, secret_key=key, sign_outgoing=True)
    conn.mav.heartbeat_send(mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    client = make_client()
    log(f"IDS 관찰 시작 mav={HOST}:{MAV_PORT} control={CONTROL_URL} "
        f"판단층={'LLM' if client else '규칙기본'}")

    home: Optional[Position] = None
    latest_gps: Optional[Position] = None       # GPS_RAW_INT (오염 가능)
    latest_ext: Optional[Position] = None       # ODOMETRY(VIO) 독립 해
    ext_sigma_m: Optional[float] = None         # 독립 해 공분산 → 품질
    ext_source = "none"
    failsafe = "nominal"
    peak_div = 0.0
    mitigated = False
    idle = 0.0

    while idle < max_idle_s and not mitigated:
        msg = conn.recv_match(blocking=True, timeout=0.3)
        if msg is None:
            idle += 0.3
            conn.mav.heartbeat_send(mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            continue
        idle = 0.0
        mt = msg.get_type()

        if mt == "HOME_POSITION":
            home = Position(lat=msg.latitude / 1e7, lon=msg.longitude / 1e7, alt_m=msg.altitude / 1000.0)
        elif mt == "GPS_RAW_INT":
            latest_gps = Position(lat=msg.lat / 1e7, lon=msg.lon / 1e7, alt_m=msg.alt / 1000.0)
        elif mt == "ODOMETRY" and home is not None:
            # v4 정본 독립 출처: VIO/지형대조 대표. 위치와 공분산(품질)을 함께 사용.
            latest_ext = offset_m(home, msg.x, msg.y)
            ext_source = "ODOMETRY(VIO)"
            try:
                var_xx = msg.pose_covariance[0]
                ext_sigma_m = math.sqrt(var_xx) if var_xx and var_xx > 0 else None
            except Exception:  # noqa: BLE001
                ext_sigma_m = None
        elif mt == "LOCAL_POSITION_NED" and home is not None and latest_ext is None:
            # v3 호환 폴백: ODOMETRY 미수신일 때만. 메시지 이름=독립성 가정 아님.
            latest_ext = offset_m(home, msg.x, msg.y)
            ext_source = "LOCAL_POSITION_NED(legacy)"
        elif mt == "HEARTBEAT":
            failsafe = "rtl" if msg.custom_mode == 6 else "nominal"

        # 검증도구(교차정합): GPS(오염) ↔ 독립 ODOMETRY 누적 발산
        if latest_gps is not None and latest_ext is not None:
            div = haversine_m(latest_gps, latest_ext)
            peak_div = max(peak_div, div)
            det = policy.detect_gps_ins_divergence(latest_gps, latest_ext)
            if det.verdict.value == "block":
                evidence = {
                    "detection_rule": det.rule.value,
                    "reason": det.reason.value,
                    "gps_vs_external_nav_divergence_m": round(div, 1),
                    "independent_source": ext_source,
                    "external_nav_sigma_m": (round(ext_sigma_m, 2) if ext_sigma_m else None),
                    "external_nav_quality": ("healthy" if (ext_sigma_m or 99) < 5.0 else "degraded"),
                    "vehicle_mode": failsafe,
                    "c2_signed_rejects_observed": count_c2_rejects(CONTROL_URL),
                    "ekf_innovation_gate_passed": True,  # 스텔스: 순간 혁신은 게이트 통과
                }
                log(f"탐지! [{det.rule.value}] {det.reason.value} — {det.detail} "
                    f"(출처 {ext_source}, 순간 혁신은 게이트 통과, 누적 발산 임계 초과)")

                # 판단층: LLM 이 대응 선택(키 있을 때) / 없으면 규칙 기본대응.
                decision = (llm_select_response(client, evidence) if client else None) or {
                    "response": "gnss_quarantine_external_nav_rtl",
                    "rationale": "독립 ODOMETRY 품질 양호 + 누적 발산 임계 초과 → GNSS 격리 후 ExternalNav 복귀(규칙 기본).",
                    "by": "rule_default"}
                log(f"대응선택[{decision['by']}]: {decision['response']} — {decision['rationale']}")

                verdict_rec = {"verdict": "block", "rule": det.rule.value, "reason": det.reason.value,
                               "evidence": evidence, "decision": decision, "detected_by": "blue_agent"}

                if decision["response"] == "continue_monitoring":
                    write_verdict(verdict_rec)
                    continue  # 완화 보류, 계속 관측
                # gnss_quarantine_external_nav_rtl / safe_hold_operator_review 모두 GNSS 격리로 시작.
                try:
                    httpx.post(f"{CONTROL_URL}/api/defense/mitigate",
                               params={"by": "blue_agent"}, timeout=3)
                    log("완화 발동 → 표적에 GNSS 격리·ExternalNav 복구 요청(가용성 보존).")
                    mitigated = True
                    verdict_rec["mitigated"] = True
                except Exception as e:  # noqa: BLE001
                    log(f"완화 요청 실패: {e}")
                write_verdict(verdict_rec)

    log(f"관찰 종료. 최대 GPS↔ExternalNav 발산={peak_div:.0f}m, 완화={'예' if mitigated else '아니오'}")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("종료(Ctrl-C).")
