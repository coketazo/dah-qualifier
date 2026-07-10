"""
common/wire.py — 공용 값객체·어휘(Enum) + 시간 헬퍼.

실물 와이어 프로토콜은 MAVLink(mavproto 패키지)다. 이 파일은 그 위에서 우리 코드가
공유하는 **중립 값객체**(위치)와 **어휘 Enum**(모드·페일세이프·탐지 사유·규칙·판정),
그리고 로그 타임스탬프 헬퍼만 담는다. 방어 결정 로직·임계값은 common/policy.py.

MAVLink 이 공개 표준이듯, 이 어휘도 red/blue/mock 이 공유해도 그레이박스가 깨지지 않는다
(포맷·어휘는 공개, 방어 '임계값/정책'만 policy.py 에 숨긴다).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel


# ─────────────────────────── 값 객체 ───────────────────────────
class Position(BaseModel):
    lat: float
    lon: float
    alt_m: float = 0.0


# ─────────────────────────── 어휘(Enum) ───────────────────────────
class Mode(str, Enum):
    MANUAL = "MANUAL"
    POSITION = "POSITION"
    AUTO = "AUTO"
    LOITER = "LOITER"
    RTL = "RTL"          # 페일세이프 자동복귀 (② 시나리오 핵심 상태)
    OFFBOARD = "OFFBOARD"


class FailsafeState(str, Enum):
    NOMINAL = "nominal"
    LINK_DEGRADED = "link_degraded"   # 임계 근처, 아직 트립 전
    TRIGGERED = "triggered"           # 페일세이프 발동(RTL 개시)


class Reason(str, Enum):
    """서버/블루가 verdict 에 기록하는 사유(어휘). 임계값은 policy 에."""
    OK = "ok"
    UNAUTHENTICATED = "unauthenticated"          # 미서명 상태변경 명령
    GPS_INS_DIVERGENCE = "gps_ins_divergence"    # GPS↔관성 교차정합 발산
    HOME_PIN_MISMATCH = "home_pin_mismatch"
    FAILSAFE_GPS_CORRELATION = "failsafe_gps_correlation"


class Verdict(str, Enum):
    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"


class Rule(str, Enum):
    """블루가 발동한 탐지 규칙(어휘). 규칙의 '내용/임계'는 policy.py 에만 존재."""
    AUTH = "auth"                                          # MAVLink2 서명 검증
    CROSS_SOURCE_CONSISTENCY = "cross_source_consistency"  # GPS↔INS 누적 발산
    HOME_PIN = "home_pin"                                  # home 벡터 vs 고정 H0
    FAILSAFE_GPS_CORRELATION = "failsafe_gps_correlation"  # 페일세이프↔GPS점프 시간상관


# ─────────────────────────── 시간 헬퍼 ───────────────────────────
def now_ts() -> str:
    """ISO-8601 UTC, 밀리초 + Z (로그·verdict 타임스탬프)."""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
