"""
mav_server — 표적(기체)의 MAVLink C2 신뢰경계 + 센서 모사 경계 + 감사 로그.

autopilot(모델)을 서로 다른 두 UDP MAVLink 신뢰영역에 연결한다:
  - C2 링크(:14550): 실제 공격/관측 표면. secure 배치에서는 RADIO_STATUS를 제외한
                     미인증 인바운드 프레임을 링크 경계에서 거부한다.
  - 센서 모사(:14600, localhost): 물리 GNSS RF 스푸핑을 GPS_INPUT으로 변환하는
                                  시뮬레이터 전용 포트. C2 서명의 우회로가 아니다.
  - tick 마다 EKF/페일세이프 전진 후 텔레메트리 8종을 전 클라이언트로 브로드캐스트.
  - 인바운드 처리:
      COMMAND_LONG : 서명 인증성 검사 → autopilot.handle_command → COMMAND_ACK 회신.
                     secure(서명강제) 배치면 미인증 명령 거부(= 명령 위·변조 방어).
      GPS_INPUT    : insecure C2 구성 또는 센서 모사 포트에서만 외부 GPS 입력으로 수용.
      HEARTBEAT    : 오퍼레이터 생존 갱신(FS_GCS 판단).
      RADIO_STATUS : 링크 품질 갱신.
  - 감사 로그(events.jsonl): 명령/주입/판정/텔레메트리 스냅샷 → 대시보드·보고서 아티팩트.

thread 하나가 링크를 돌린다. app(FastAPI)가 start()/mitigate()/truth() 등을 호출.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from mavproto.dialect import ids
from mavproto.signing import derive_key
from mavproto.link import MavServer
from common.wire import now_ts
from .autopilot import Autopilot

OPERATOR_PASSPHRASE = os.environ.get("MAV_OPER_KEY", "OPER-SECRET-2026")
TELEM_LOG_EVERY = 5   # N tick 마다 텔레메트리 스냅샷 로깅(과다 로그 방지)


class MockGCSServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 14550,
                 sensor_host: str = "127.0.0.1", sensor_port: int = 14600,
                 secure: bool = False, log_path: str = "logs/events.jsonl"):
        self.host, self.port = host, port
        self.sensor_host, self.sensor_port = sensor_host, sensor_port
        self.secure = secure
        self.log_path = log_path
        self.key = derive_key(OPERATOR_PASSPHRASE)
        self.ap = Autopilot(secure=secure)
        self.c2_link: Optional[MavServer] = None
        self.sensor_link: Optional[MavServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        self._tick = 0
        self._recent: list[dict] = []      # 대시보드용 인메모리 링버퍼
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("", encoding="utf-8")

    # ─────────────────────────── 로그 ───────────────────────────
    def _log(self, event: dict) -> None:
        self._seq += 1
        event = {"seq": self._seq, "ts": now_ts(), **event}
        with Path(self.log_path).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._recent.append(event)
        if len(self._recent) > 200:
            self._recent = self._recent[-200:]

    def recent(self, n: int = 30) -> list[dict]:
        return self._recent[-n:]

    # ─────────────────────────── 수명주기 ───────────────────────────
    def start(self) -> None:
        self.c2_link = MavServer(self.host, self.port, me=ids.vehicle,
                                 secret_key=self.key, require_signing=self.secure)
        # 물리 센서 환경을 모사하는 내부 포트다. 외부 배포/EXPOSE 대상이 아니다.
        self.sensor_link = MavServer(self.sensor_host, self.sensor_port,
                                     me=ids.sensor_emulator, secret_key=None,
                                     require_signing=False)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.c2_link:
            self.c2_link.close()
        if self.sensor_link:
            self.sensor_link.close()

    def reset(self) -> None:
        self.ap.reset()
        self._seq = 0
        self._tick = 0
        self._recent.clear()
        Path(self.log_path).write_text("", encoding="utf-8")

    def mitigate(self, by: str = "blue_agent") -> None:
        self.ap.mitigate(by=by)
        self._log({"event": "verdict", "verdict": "block", "rule": "cross_source_consistency",
                   "reason": "gps_ins_divergence", "response": "gnss_quarantine_external_nav_rtl",
                   "detected_by": by, "availability_impact": 0})

    def safe_hold(self, by: str = "blue_agent") -> None:
        """대체 대응: ExternalNav 품질 불충분 → RTL 대신 안전 LOITER + 운용자 인계."""
        self.ap.safe_hold(by=by)
        self._log({"event": "verdict", "verdict": "block", "rule": "cross_source_consistency",
                   "reason": "gps_ins_divergence", "response": "safe_hold_operator_review",
                   "detected_by": by, "availability_impact": 0})

    def degrade_extnav(self, sigma_m: float = 25.0, source: str = "scenario_harness") -> None:
        """독립 ExternalNav(VIO) 품질 저하 환경효과(특징희소 지형·저조도 등)."""
        self.ap.degrade_external_nav(sigma_m)
        self._log({"event": "inject", "type": "extnav_quality_degrade", "source": source,
                   "params": {"sigma_m": sigma_m}, "malicious": False})

    def degrade_link(self, quality: float = 0.1, hold_s: float = 6.0,
                     source: str = "scenario_harness") -> None:
        """부차 벡터: RF 링크 열화(페일세이프 유도). RF 계층은 프로토콜 메시지가 아니라
        환경효과이므로 제어평면으로 노출(정직한 추상화)."""
        self.ap.set_link_quality(quality)
        self._link_restore_at = time.monotonic() + hold_s
        self._log({"event": "inject", "type": "link_degrade", "source": source,
                   "params": {"quality": quality, "hold_s": hold_s}, "malicious": True})

    _link_restore_at = 0.0

    # ─────────────────────────── 메인 루프 ───────────────────────────
    def _run(self) -> None:
        assert self.c2_link is not None and self.sensor_link is not None
        while not self._stop.is_set():
            t0 = time.monotonic()
            # 오퍼레이터 GCS 는 정상 생존(잼 없으면). 링크 열화 창 복원.
            if self._link_restore_at and t0 >= self._link_restore_at:
                self.ap.set_link_quality(1.0)
                self._link_restore_at = 0.0
            if self.ap.link_quality >= 0.3:
                self.ap.set_operator_heartbeat()

            self._handle_c2_inbound()
            self._handle_sensor_inbound()
            self.ap.step()
            for msg in self.ap.telemetry_messages():
                self.c2_link.broadcast(msg)

            self._tick += 1
            if self._tick % TELEM_LOG_EVERY == 0:
                t = self.ap.truth()
                self._log({"event": "telemetry", "mode": t["mode"], "failsafe": t["failsafe"],
                           "ekf_pos_horiz_var": t["ekf_pos_horiz_var"],
                           "estimate_true_bias_m": t["estimate_true_bias_m"],
                           "spoof_active": t["spoof_active"], "defended": t["defended"],
                           "defense_state": t["defense_state"], "nav_source": t["nav_source"],
                           "mission_compromised": t["mission_compromised"],
                           "platform_availability": t["platform_availability"],
                           "c2_availability": t["c2_availability"],
                           "availability": t["availability"]})

            dt = self.ap.tick_s - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)

    def _handle_c2_inbound(self) -> None:
        assert self.c2_link is not None
        for rx in self.c2_link.poll():
            mtype = rx.msg.get_type()
            # ArduPilot 실배치 정합: signed C2 링크에서는 RADIO_STATUS 예외 외의
            # 미서명/위조서명 인바운드 패킷을 메시지 종류와 무관하게 무시한다.
            if self.secure and mtype != "RADIO_STATUS" and not rx.authentic:
                sensor_message = mtype == "GPS_INPUT"
                self.ap.record_c2_reject(sensor_message=sensor_message)
                self._log({"event": "mav_rx_reject", "trust_domain": "c2",
                           "message_type": mtype,
                           "src_system": rx.msg.get_srcSystem(),
                           "src_component": rx.msg.get_srcComponent(),
                           "signed": rx.signed, "authentic": rx.authentic,
                           "reason": rx.auth_reason})
                continue
            if mtype == "COMMAND_LONG":
                self._on_command(rx)
            elif mtype == "GPS_INPUT":
                self._on_gps_input(rx, trust_domain="c2_insecure")
            elif mtype == "HEARTBEAT":
                # 서명된 오퍼레이터 하트비트만 생존 근거로 인정(사칭 방지)
                if rx.authentic or not self.secure:
                    self.ap.set_operator_heartbeat()
            elif mtype == "RADIO_STATUS":
                q = max(0.0, min(1.0, rx.msg.rssi / 255.0))
                self.ap.set_link_quality(q)

    def _handle_sensor_inbound(self) -> None:
        """물리 GNSS 환경 모사 입력. C2 인증정책과 섞지 않는다."""
        assert self.sensor_link is not None
        for rx in self.sensor_link.poll():
            if rx.msg.get_type() == "GPS_INPUT":
                self._on_gps_input(rx, trust_domain="gnss_rf_emulator")
            else:
                self._log({"event": "sensor_rx_reject", "trust_domain": "sensor_sim",
                           "message_type": rx.msg.get_type(), "reason": "not_gnss_measurement"})

    def _on_command(self, rx) -> None:
        cmd = rx.msg.command
        accepted, result = self.ap.handle_command(cmd, authentic=rx.authentic, signed=rx.signed)
        # COMMAND_ACK 회신(요청자에게)
        ack = mavlink.MAVLink_command_ack_message(cmd, result, 0, 0,
                                                  rx.msg.get_srcSystem(), rx.msg.get_srcComponent())
        try:
            self.c2_link.sock.sendto(ack.pack(self.c2_link._tx), rx.addr)
        except OSError:
            pass
        self._log({"event": "command", "command": int(cmd),
                   "src_system": rx.msg.get_srcSystem(), "src_component": rx.msg.get_srcComponent(),
                   "signed": rx.signed, "authentic": rx.authentic,
                   "accepted": accepted, "result": int(result),
                   "malicious": not rx.authentic})

    def _on_gps_input(self, rx, *, trust_domain: str) -> None:
        lat = rx.msg.lat / 1e7
        lon = rx.msg.lon / 1e7
        alt = float(rx.msg.alt)
        self.ap.inject_gps_input(lat, lon, alt)
        self._log({"event": "inject", "type": "gps_input_spoof",
                   "trust_domain": trust_domain,
                   "src_system": rx.msg.get_srcSystem(), "src_component": rx.msg.get_srcComponent(),
                   "lat": lat, "lon": lon, "malicious": True})

    # ─────────────────────────── 조회 ───────────────────────────
    def truth(self) -> dict:
        return self.ap.truth()

    @property
    def link(self) -> Optional[MavServer]:
        """v3 app 호환 별칭. 신규 코드는 c2_link를 사용한다."""
        return self.c2_link
