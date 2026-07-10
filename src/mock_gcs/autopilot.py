"""
autopilot — 축소차수 기체 + 재귀 EKF 추정기 + 페일세이프 상태기계 (트랙 A 핵심).

방산 현업 매핑:
  - true_pos     : 실제 세계 위치(지상 진실, 무선으로 안 나감).
  - extnav_pos   : GNSS 와 독립된 보조 항법 해(VIO/지형대조를 축소 모사).
                   ODOMETRY 로 명시하고, LOCAL_POSITION_NED 는 기존 에이전트 호환용으로 병행 송출.
  - gps_meas     : GPS 측정. 정상이면 true, 공격 시 GPS_INPUT 주입값(스푸핑).
  - ekf_pos      : EKF 융합 추정(주 항법). GLOBAL_POSITION_INT 로 송출 = 오퍼레이터 화면.
  - 재귀 EKF     : 예측=직전 ekf_pos. 혁신=gps_meas-예측. 게이트 이내면 융합(K).
                   → '느린' 스푸핑은 혁신을 게이트 아래로 유지해 편이를 누적(스텔스).
                   빠른 스푸핑은 혁신 게이트/FS_EKF 에 걸린다(온보드 기본 방어).
  - 교차정합 근거 : 느린 스푸핑에서도 gps_meas↔extnav_pos 누적 발산은 자란다 → blue IDS 가 잡는다.

이 파일은 순수 모델 + MAVLink 메시지 팩토리. UDP 입출력은 mav_server 가 담당한다.
스레드 안전: 외부(mav_server)가 self.lock 을 잡고 호출.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from common.wire import Position, Mode, FailsafeState
from common.geo import haversine_m, offset_m, north_east_m

# ─────────────────────────── 펌웨어(기체) 파라미터 = 온보드 설정 ───────────────────────────
EKF_INNOV_GATE_M = 12.0        # 혁신 게이트: 순간 혁신 > 이 값이면 GPS 스텝 기각(빠른 스푸핑 차단)
EKF_FUSION_GAIN = 0.85         # 칼만 이득 근사(게이트 내 GPS 추종 강도)
FS_EKF_THRESH = 0.8            # 정규화 분산 > 이 값 지속 시 EKF 페일세이프(ArduPilot FS_EKF_THRESH)
FS_EKF_DURATION_S = 2.0
FS_GCS_TIMEOUT_S = 3.0         # 오퍼레이터 하트비트 상실 → GCS 페일세이프(RTL)
CRUISE_SPEED_M_S = 22.0
EXTNAV_MAX_BIAS_M = 2.0       # 독립 ExternalNav 오차의 유계 모사(VIO/지형대조)
RECOVERY_HOLD_S = 2.0         # GNSS 격리 후 안전 LOITER 유지 시간
MISSION_COMPROMISE_M = 100.0  # 단순 EKF 게이트가 아닌 임무 무결성 상실 판정선


class Autopilot:
    def __init__(self, *, secure: bool = False):
        self.lock = threading.RLock()
        self.secure = secure
        self.tick_s = 0.2
        self.boot_ms = int(time.time() * 1000)
        self.reset()

    # ─────────────────────────── 상태 초기화 ───────────────────────────
    def reset(self) -> None:
        with self.lock:
            self.sysid = 1
            self.home = Position(lat=37.5000, lon=127.0000, alt_m=0.0)
            self.true_pos = offset_m(self.home, 3000.0, 2000.0); self.true_pos.alt_m = 300.0
            self.extnav_pos = Position(**self.true_pos.model_dump())
            self.ekf_pos = Position(**self.true_pos.model_dump())
            self.gps_meas = Position(**self.true_pos.model_dump())
            # 임무: home 북동 6km 정찰 웨이포인트로 자동 비행(AUTO)
            self.target = offset_m(self.home, 6000.0, 4000.0); self.target.alt_m = 300.0
            self.mode = Mode.AUTO
            self.armed = True
            self.failsafe = FailsafeState.NOMINAL

            # GPS 주입(스푸핑) 상태
            self.ext_gps_active = False       # GPS_INPUT 주입 수신 중인가
            self.ext_gps_pos: Optional[Position] = None
            self.last_gps_input_ts: Optional[float] = None
            self.attack_observed = False      # 실제 스푸핑이 한 번이라도 있었나(정탐/오탐 판정)
            self.gps_jammed = False
            self.sim_time_s = 0.0

            # EKF 상태
            self.using_gps = True             # GPS 융합 레인 사용(방어 시 False = 관성 폴백)
            self.nav_source = "GNSS"
            self.pos_horiz_var = 0.0          # 정규화 수평위치 분산(EKF_STATUS_REPORT)
            self.vel_var = 0.0
            self._ekf_bad_since: Optional[float] = None

            # 링크/GCS 하트비트
            self.link_quality = 1.0
            self.last_op_heartbeat = time.monotonic()
            self._link_below_since: Optional[float] = None

            # 방어/채점
            self.defended = False
            self.defended_by: Optional[str] = None
            self.defense_state = "MONITORING"
            self._mitigation_started_s: Optional[float] = None
            self.over_blocked = False
            self.platform_availability = 100.0
            self.c2_availability = 100.0

            # 카운터
            self.rejected_unsigned = 0
            self.rejected_c2_sensor = 0
            self.accepted_commands = 0

    # ─────────────────────────── 파생/유틸 ───────────────────────────
    def _time_boot_ms(self) -> int:
        return int(time.time() * 1000) - self.boot_ms

    def _lat_e7(self, p: Position) -> int:
        return int(p.lat * 1e7)

    def _lon_e7(self, p: Position) -> int:
        return int(p.lon * 1e7)

    # ─────────────────────────── 인바운드: 명령 평면 ───────────────────────────
    def handle_command(self, command: int, authentic: bool, signed: bool) -> tuple[bool, int]:
        """COMMAND_LONG 처리. secure(서명강제) 배치면 미인증 명령 거부.
        반환: (accepted, MAV_RESULT)."""
        with self.lock:
            if self.secure and not authentic:
                self.rejected_unsigned += 1
                return False, mavlink.MAV_RESULT_DENIED
            self.accepted_commands += 1
            if command == mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH:
                self._engage_rtl(reason="operator_cmd")
            return True, mavlink.MAV_RESULT_ACCEPTED

    # ─────────────────────────── 인바운드: GPS_INPUT 주입(스푸핑 벡터) ───────────────────────────
    def inject_gps_input(self, lat: float, lon: float, alt_m: float) -> None:
        """외부 GPS(GPS_INPUT) 주입. GPS_TYPE=MAV 구성/무검증 수용 취약점의 실물 벡터.
        공격자는 현재 보고위치 근처로 조금씩 편이시켜 게이트 아래 스텔스 스푸핑을 만든다."""
        with self.lock:
            self.ext_gps_active = True
            self.ext_gps_pos = Position(lat=lat, lon=lon, alt_m=alt_m)
            self.last_gps_input_ts = time.monotonic()
            self.attack_observed = True

    def set_operator_heartbeat(self) -> None:
        with self.lock:
            self.last_op_heartbeat = time.monotonic()

    def set_link_quality(self, q: float) -> None:
        with self.lock:
            self.link_quality = max(0.0, min(1.0, q))
            self.c2_availability = round(self.link_quality * 100.0, 1)

    def record_c2_reject(self, *, sensor_message: bool = False) -> None:
        """C2 신뢰경계에서 거부한 미인증 프레임을 채점/감사용으로 기록."""
        with self.lock:
            self.rejected_unsigned += 1
            if sensor_message:
                self.rejected_c2_sensor += 1

    def mitigate(self, by: str = "blue_agent") -> None:
        """방어 발동: GNSS 격리 → 독립 ExternalNav 안전 LOITER → RTL 복구.

        순수 INS 로 장시간 RTL 하는 모델이 아니다. 독립 보조항법이 건강하다는 전제에서
        짧은 containment 창을 거친 뒤 복구 상태기로 이행한다.
        """
        with self.lock:
            self.using_gps = False
            self.nav_source = "EXTERNAL_NAV"
            self.defended = True
            self.defended_by = by
            self.defense_state = "GNSS_QUARANTINED"
            self._mitigation_started_s = self.sim_time_s
            self.mode = Mode.LOITER
            self.target = Position(**self.extnav_pos.model_dump())

    def _engage_rtl(self, reason: str) -> None:
        self.mode = Mode.RTL
        self.failsafe = FailsafeState.TRIGGERED
        self.target = Position(**self.home.model_dump())

    # ─────────────────────────── 시뮬 틱 ───────────────────────────
    def step(self, dt: Optional[float] = None) -> None:
        with self.lock:
            dt = dt or self.tick_s
            self.sim_time_s += dt
            now = time.monotonic()

            # GNSS 와 독립된 ExternalNav(VIO/지형대조) 축소 모사. 실제값을 그대로 노출하는
            # oracle 이 아니라, 독립 센서의 유계 오차를 결정론적으로 재현한다.
            self._update_external_nav()

            # 1) GPS 측정 갱신: 주입 활성이면 주입값, 아니면 실제(true)
            if self.ext_gps_active and self.ext_gps_pos is not None:
                if self.last_gps_input_ts and (now - self.last_gps_input_ts) > 2.0:
                    self.ext_gps_active = False          # 주입 끊기면 실제 GPS 복귀
                    self.gps_meas = Position(**self.true_pos.model_dump())
                else:
                    self.gps_meas = Position(**self.ext_gps_pos.model_dump())
            else:
                self.gps_meas = Position(**self.true_pos.model_dump())

            # 2) 재귀 EKF: 예측=직전 ekf_pos. 혁신=gps_meas-예측.
            if self.using_gps and not self.gps_jammed:
                inn_n, inn_e = north_east_m(self.ekf_pos, self.gps_meas)
                innovation_m = math.hypot(inn_n, inn_e)
                self.pos_horiz_var = innovation_m / EKF_INNOV_GATE_M   # 정규화 분산
                self.vel_var = min(1.5, self.pos_horiz_var * 0.6)
                if innovation_m <= EKF_INNOV_GATE_M:                    # 게이트 내 → 융합
                    self.ekf_pos = offset_m(self.ekf_pos,
                                            EKF_FUSION_GAIN * inn_n, EKF_FUSION_GAIN * inn_e)
                # 게이트 초과: 이 스텝 GPS 기각(ekf 유지) = 빠른 스푸핑 차단
            else:
                # ExternalNav 폴백 레인: EKF 를 독립 보조항법으로 끌고감(GNSS 무시).
                pn, pe = north_east_m(self.ekf_pos, self.extnav_pos)
                self.ekf_pos = offset_m(self.ekf_pos, 0.5 * pn, 0.5 * pe)
                self.pos_horiz_var = 0.0
                self.vel_var = 0.0

            # 3) 페일세이프 + 방어 복구 상태기계
            self._update_failsafe(now, dt)
            if (self.defense_state == "GNSS_QUARANTINED"
                    and self._mitigation_started_s is not None
                    and self.sim_time_s - self._mitigation_started_s >= RECOVERY_HOLD_S):
                self.defense_state = "EXTERNAL_NAV_RTL"
                self.mode = Mode.RTL
                self.target = Position(**self.home.model_dump())

            # 4) 항법: 컨트롤러는 ekf_pos 를 믿고 target 으로 유도.
            #    실제 이동에 같은 제어가 적용 → ekf 가 스푸핑 편이면 true 는 반대로 오유도.
            if self.mode in (Mode.RTL, Mode.AUTO, Mode.LOITER):
                err_n, err_e = north_east_m(self.ekf_pos, self.target)
                dist = math.hypot(err_n, err_e)
                if dist > 1.0:
                    mv = min(CRUISE_SPEED_M_S * dt, dist)
                    fn, fe = err_n / dist * mv, err_e / dist * mv
                    self.true_pos = offset_m(self.true_pos, fn, fe)
                    self.ekf_pos = offset_m(self.ekf_pos, fn, fe)
                    self._update_external_nav()

            # 5) 플랫폼 가용성과 C2 링크 가용성을 분리한다. 링크 열화 중에도 온보드
            #    자율비행은 살아 있을 수 있으므로 단일 'SLA 100' 숫자로 뭉개지 않는다.
            self.over_blocked = self.defended and not self.attack_observed
            if self.over_blocked:
                self.platform_availability = max(0.0, self.platform_availability - 10.0 * dt)
            else:
                self.platform_availability = min(100.0, self.platform_availability + 5.0 * dt)

    def _update_external_nav(self) -> None:
        """GNSS 비의존 ExternalNav의 유계 오차를 모사한다.

        실제 배치에서는 VIO/지형대조/레이더/보조 GNSS 중 하나로 대체한다. 이 레인은
        LOCAL_POSITION_NED가 본질적으로 독립이라는 가정이 아니라, mock 계약으로 명시된
        별도 센서 출처다.
        """
        bn = EXTNAV_MAX_BIAS_M * math.sin(self.sim_time_s / 30.0)
        be = 0.6 * EXTNAV_MAX_BIAS_M * math.cos(self.sim_time_s / 37.0)
        self.extnav_pos = offset_m(self.true_pos, bn, be)

    def _update_failsafe(self, now: float, dt: float) -> None:
        # FS_EKF: 정규화 분산 지속 초과
        if self.pos_horiz_var > FS_EKF_THRESH:
            if self._ekf_bad_since is None:
                self._ekf_bad_since = now
            elif (now - self._ekf_bad_since) >= FS_EKF_DURATION_S and self.failsafe != FailsafeState.TRIGGERED:
                self._engage_rtl(reason="fs_ekf")
        else:
            self._ekf_bad_since = None

        # FS_GCS: 오퍼레이터 하트비트 상실
        gcs_gap = now - self.last_op_heartbeat
        if self.link_quality < 0.3 or gcs_gap > FS_GCS_TIMEOUT_S:
            if self._link_below_since is None:
                self._link_below_since = now
                if self.failsafe == FailsafeState.NOMINAL:
                    self.failsafe = FailsafeState.LINK_DEGRADED
            elif (now - self._link_below_since) >= 1.0 and self.failsafe != FailsafeState.TRIGGERED:
                self._engage_rtl(reason="fs_gcs")
        else:
            self._link_below_since = None
            if self.failsafe == FailsafeState.LINK_DEGRADED:
                self.failsafe = FailsafeState.NOMINAL

    # ─────────────────────────── MAVLink 메시지 팩토리(송출) ───────────────────────────
    def msg_heartbeat(self):
        base = mavlink.MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0
        base |= mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        return mavlink.MAVLink_heartbeat_message(
            mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base, self._custom_mode(), mavlink.MAV_STATE_ACTIVE, 3)

    def _custom_mode(self) -> int:
        # ArduCopter 커스텀 모드 매핑(대표값): LOITER=5, AUTO=3, RTL=6
        return {Mode.LOITER: 5, Mode.AUTO: 3, Mode.RTL: 6}.get(self.mode, 5)

    def msg_gps_raw_int(self):
        p = self.gps_meas
        fix = mavlink.GPS_FIX_TYPE_NO_FIX if self.gps_jammed else mavlink.GPS_FIX_TYPE_3D_FIX
        return mavlink.MAVLink_gps_raw_int_message(
            self._time_boot_ms() * 1000, fix, self._lat_e7(p), self._lon_e7(p),
            int(p.alt_m * 1000), 65535, 65535, 0, 65535, 0 if self.gps_jammed else 11)

    def msg_global_position_int(self):
        p = self.ekf_pos                       # 오퍼레이터가 보는 융합 추정
        return mavlink.MAVLink_global_position_int_message(
            self._time_boot_ms(), self._lat_e7(p), self._lon_e7(p),
            int(p.alt_m * 1000), int(p.alt_m * 1000), 0, 0, 0, 65535)

    def msg_local_position_ned(self):
        # 기존 에이전트 호환 스트림. 독립성의 정본 표시는 아래 ODOMETRY 메시지다.
        n, e = north_east_m(self.home, self.extnav_pos)
        return mavlink.MAVLink_local_position_ned_message(
            self._time_boot_ms(), float(n), float(e), float(-self.extnav_pos.alt_m),
            0.0, 0.0, 0.0)

    def msg_odometry(self):
        """독립 ExternalNav 레인(VIO/지형대조 축소 모사)을 출처까지 명시해 송출."""
        n, e = north_east_m(self.home, self.extnav_pos)
        pose_cov = [float("nan")] * 21
        vel_cov = [float("nan")] * 21
        pose_cov[0] = pose_cov[6] = 4.0  # 약 2m 1-sigma 수평 오차
        pose_cov[11] = 9.0
        return mavlink.MAVLink_odometry_message(
            self._time_boot_ms() * 1000, mavlink.MAV_FRAME_LOCAL_NED,
            mavlink.MAV_FRAME_BODY_FRD, float(n), float(e), float(-self.extnav_pos.alt_m),
            [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            pose_cov, vel_cov, 0, mavlink.MAV_ESTIMATOR_TYPE_VIO, 90)

    def msg_ekf_status_report(self):
        flags = (mavlink.ESTIMATOR_ATTITUDE | mavlink.ESTIMATOR_VELOCITY_HORIZ |
                 mavlink.ESTIMATOR_POS_HORIZ_REL | mavlink.ESTIMATOR_PRED_POS_HORIZ_REL)
        if self.using_gps and not self.gps_jammed:
            flags |= mavlink.ESTIMATOR_POS_HORIZ_ABS
        return mavlink.MAVLink_ekf_status_report_message(
            flags, float(self.vel_var), float(self.pos_horiz_var),
            0.0, 0.0, 0.0, 0.0)

    def msg_home_position(self):
        h = self.home
        return mavlink.MAVLink_home_position_message(
            self._lat_e7(h), self._lon_e7(h), int(h.alt_m * 1000),
            0.0, 0.0, 0.0, [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0, self._time_boot_ms() * 1000)

    def msg_sys_status(self):
        return mavlink.MAVLink_sys_status_message(
            0, 0, 0, int((self.platform_availability / 100.0) * 500),
            12000, 500, int(self.platform_availability),
            0, 0, 0, 0, 0, 0)

    def telemetry_messages(self) -> list:
        with self.lock:
            return [self.msg_heartbeat(), self.msg_gps_raw_int(),
                    self.msg_global_position_int(), self.msg_local_position_ned(),
                    self.msg_odometry(), self.msg_ekf_status_report(), self.msg_home_position(),
                    self.msg_sys_status()]

    # ─────────────────────────── 진실/관측(대시보드·채점) ───────────────────────────
    def truth(self) -> dict:
        with self.lock:
            bias_m = haversine_m(self.ekf_pos, self.true_pos)
            mission_compromised = (bias_m >= MISSION_COMPROMISE_M and not self.defended)
            return {
                "true_position": self.true_pos.model_dump(),
                "ekf_position": self.ekf_pos.model_dump(),
                "gps_measurement": self.gps_meas.model_dump(),
                "external_nav_position": self.extnav_pos.model_dump(),
                "ins_position": self.extnav_pos.model_dump(),  # v3 호환 별칭; 신규 문서에서는 사용 금지
                "home": self.home.model_dump(),
                "mode": self.mode.value,
                "failsafe": self.failsafe.value,
                "ekf_pos_horiz_var": round(self.pos_horiz_var, 3),
                "estimate_true_bias_m": round(bias_m, 1),
                "spoof_active": self.ext_gps_active,
                "nav_source": self.nav_source,
                "defended": self.defended,
                "defended_by": self.defended_by,
                "defense_state": self.defense_state,
                "rejected_unsigned": self.rejected_unsigned,
                "rejected_c2_sensor": self.rejected_c2_sensor,
                "platform_availability": int(self.platform_availability),
                "c2_availability": int(self.c2_availability),
                "availability": int(self.platform_availability),  # v3 호환 별칭
                "mission_compromised": mission_compromised,
                "hijacked": mission_compromised,  # v3 호환 별칭
            }
