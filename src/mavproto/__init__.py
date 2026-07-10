"""
mavproto — DAH 2026 공용 MAVLink 프로토콜 인프라.

MAVLink 2.0(표준 ardupilotmega 다이얼렉트) 위에서 표적(mock_gcs)·공격자(red)·방어자(blue)가
동일한 실물 규약으로 통신한다. 이 패키지는 '전송·서명·식별' 인프라만 제공한다
(wire.py 가 로그 포맷을, policy.py 가 방어 결정을 담당하는 것과 같은 계층 분리).

- dialect : 표준 다이얼렉트 핸들 + 방산 관례의 시스템/컴포넌트 ID
- signing : MAVLink2 메시지 서명(HMAC-SHA256) 키 파생·검증 — 실물 인증 메커니즘
- link    : UDP 멀티클라이언트 서버(표적) + 에이전트 접속 헬퍼

주의: MAVLink2(서명 지원)를 쓰려면 mavutil import 이전에 MAVLINK20 을 켜야 한다.
"""
from __future__ import annotations

import os

# mavutil 은 import 시점 환경변수로 와이어 버전을 고정한다. 반드시 아래 import 보다 먼저.
os.environ.setdefault("MAVLINK20", "1")

from .dialect import mavlink, ids, MAVLINK_IFLAG_SIGNED  # noqa: E402,F401
