"""
표준 MAVLink 2.0 다이얼렉트 핸들 + 방산 관례 식별자.

커스텀 다이얼렉트를 새로 만들지 않는다 — 표준 `ardupilotmega`(common의 상위집합)를
그대로 쓰는 것이 현업 정석이며, 우리가 규약을 발명한 게 아니라 '실물 규약을 쓴다'는
사실을 보증한다. 실 지상관제(QGroundControl/MAVProxy)·오토파일럿(ArduPilot/PX4)이
쓰는 바로 그 메시지 집합이다.
"""
from __future__ import annotations

from dataclasses import dataclass

from pymavlink.dialects.v20 import ardupilotmega as mavlink

# MAVLink2 헤더 incompat_flags 의 서명 비트(0x01). 수신 프레임이 서명됐는지 판정.
MAVLINK_IFLAG_SIGNED = 0x01

# 프로토콜 상수(가독성용 재노출)
FIX_3D = mavlink.GPS_FIX_TYPE_3D_FIX
RESULT_ACCEPTED = mavlink.MAV_RESULT_ACCEPTED
RESULT_DENIED = mavlink.MAV_RESULT_DENIED
CMD_RTL = mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH
CMD_SET_MODE = mavlink.MAV_CMD_DO_SET_MODE


@dataclass(frozen=True)
class MavId:
    """MAVLink 노드 식별(system_id, component_id). 방산 관례 값 사용."""
    system: int
    component: int
    role: str


@dataclass(frozen=True)
class _Ids:
    # 기체측 오토파일럿(진짜 발신원). ArduPilot 관례: sys=1, comp=AUTOPILOT1(1).
    vehicle: MavId = MavId(1, mavlink.MAV_COMP_ID_AUTOPILOT1, "autopilot")
    # 정규 오퍼레이터 지상국. 관례: sys=255, comp=MISSIONPLANNER(190).
    operator: MavId = MavId(255, mavlink.MAV_COMP_ID_MISSIONPLANNER, "operator_gcs")
    # 공격자: 정규 GCS의 sys/comp 를 '사칭'한다(서명 없으면 구분 불가 = 취약점의 본질).
    red: MavId = MavId(255, mavlink.MAV_COMP_ID_MISSIONPLANNER, "red_agent")
    # 방어자: 수동 관측 노드(온보드 컴퓨터 관례 comp). 별도 sys 로 구분.
    blue: MavId = MavId(254, mavlink.MAV_COMP_ID_ONBOARD_COMPUTER, "blue_agent")
    # 시뮬레이터 내부 GNSS RF 환경 어댑터. C2 노드가 아니며 외부 배포 대상도 아니다.
    sensor_emulator: MavId = MavId(42, mavlink.MAV_COMP_ID_GPS, "gnss_rf_emulator")


ids = _Ids()
