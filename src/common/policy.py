"""
DAH 2026 방어 정책 — blue_agent 전용. **red_agent 는 import 금지(그레이박스).**

여기 담긴 것 = 방어의 '결정 로직·임계값'. red 는 이걸 모르고 서버 응답을 관찰해
경험적으로 추론해야 한다.

방어 계층:
  1) 명령 인증(1차)   : MAVLink2 서명 검증. **오토파일럿(mock_gcs/autopilot)에서 강제** —
                        미서명 상태변경 명령 거부. (정책이라기보다 펌웨어 설정이라 여기 없음)
  2) 텔레메트리 교차정합(2차, 여기): GPS↔INS 누적 발산 등으로 스텔스 스푸핑 탐지.
                        스텔스 스푸핑은 순간 EKF 혁신을 게이트 아래로 유지하지만
                        GPS↔INS '누적' 발산은 자란다 → 그걸 잡는다.

현재 blue 가 상시 쓰는 것: detect_gps_ins_divergence(교차정합, 1차 신호).
home_pin·failsafe_gps_correlation·evaluate_telemetry 는 다신호 방어 라이브러리로,
본선에서 blue triage 가 종합에 사용(예선은 교차정합만으로 증거 충분).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .wire import FailsafeState, Position, Reason, Rule, Verdict
from .geo import haversine_m

# ─────────────────────────── 텔레메트리 교차정합 임계값 ───────────────────────────
GPS_INS_DIVERGENCE_TOLERANCE_M = 50.0     # GPS↔관성(INS) 누적 발산 허용치(스텔스 스푸핑 탐지)
HOME_PIN_TOLERANCE_M = 30.0               # 보고 home vs 고정 H0 허용치
FAILSAFE_GPS_CORR_WINDOW_S = 5.0          # 페일세이프 트립↔GPS 점프 상관 창


@dataclass
class Detection:
    verdict: Verdict
    rule: Optional[Rule]
    reason: Reason
    detail: str = ""


def detect_gps_ins_divergence(reported: Position, ins: Position) -> Detection:
    """GPS 보고 위치와 INS 독립 추정이 임계 초과로 벌어지면 스푸핑 의심(1차 신호)."""
    d = haversine_m(reported, ins)
    if d > GPS_INS_DIVERGENCE_TOLERANCE_M:
        return Detection(Verdict.BLOCK, Rule.CROSS_SOURCE_CONSISTENCY,
                         Reason.GPS_INS_DIVERGENCE, f"GPS-INS {d:.0f}m 발산")
    return Detection(Verdict.ALLOW, None, Reason.OK, f"정합 {d:.0f}m")


def detect_home_pin_mismatch(reported_home: Position, pinned_home: Position) -> Detection:
    """RTL 이 향하는 home 이 최초 고정 H0 와 다르면 공격(다신호 라이브러리)."""
    d = haversine_m(reported_home, pinned_home)
    if d > HOME_PIN_TOLERANCE_M:
        return Detection(Verdict.BLOCK, Rule.HOME_PIN, Reason.HOME_PIN_MISMATCH,
                         f"home {d:.0f}m 이동")
    return Detection(Verdict.ALLOW, None, Reason.OK, "home 일치")


def detect_failsafe_gps_correlation(failsafe: FailsafeState,
                                    seconds_since_gps_jump: Optional[float]) -> Detection:
    """페일세이프(RTL) 트립 직후 GPS 점프 = 그린보드 하이재킹 지문(다신호 라이브러리)."""
    if (failsafe == FailsafeState.TRIGGERED and seconds_since_gps_jump is not None
            and seconds_since_gps_jump <= FAILSAFE_GPS_CORR_WINDOW_S):
        return Detection(Verdict.BLOCK, Rule.FAILSAFE_GPS_CORRELATION,
                         Reason.FAILSAFE_GPS_CORRELATION, "페일세이프↔GPS점프 상관")
    return Detection(Verdict.ALLOW, None, Reason.OK, "상관 없음")


def evaluate_telemetry(reported: Position, ins: Position,
                       reported_home: Position, pinned_home: Position,
                       failsafe: FailsafeState,
                       seconds_since_gps_jump: Optional[float]) -> Detection:
    """다신호 종합 판정: 가장 강한 탐지 반환(BLOCK 우선). 본선 triage 진입점."""
    for det in (
        detect_gps_ins_divergence(reported, ins),
        detect_home_pin_mismatch(reported_home, pinned_home),
        detect_failsafe_gps_correlation(failsafe, seconds_since_gps_jump),
    ):
        if det.verdict == Verdict.BLOCK:
            return det
    return Detection(Verdict.ALLOW, None, Reason.OK, "정상")
