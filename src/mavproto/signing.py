"""
MAVLink2 메시지 서명(HMAC-SHA256) — 실물 인증 메커니즘.

MAVLink2 는 각 프레임 끝에 (link_id, 48bit timestamp, 6byte 서명) 트레일러를 붙인다.
서명 = sha256(secret_key + header + payload + crc + link_id + timestamp)[:6].
32바이트 공유 비밀키를 아는 노드만 유효 서명을 만들 수 있다 → 명령 위·변조 방지.

방산 매핑:
  - 정규 오퍼레이터 GCS 와 기체는 사전 배포된 공유키를 갖는다.
  - 공격자(red)는 키가 없다 → 미서명 또는 위조서명만 가능(둘 다 검증에서 걸린다).
  - '서명 강제(require_signing)'가 꺼진 배치 = 실전 오설정 취약점(무인증 MAVLink).

구현 주의: 다수 클라이언트가 한 소켓을 공유하면 pymavlink 파서의 링크별 timestamp
추적이 정상 프레임까지 드롭한다. 그래서 수신 파서는 키를 쥐지 않고(드롭 방지),
인증 검증은 프레임 단위로 아래 verify_signature() 로 독립 수행한다.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from pymavlink.dialects.v20 import ardupilotmega as _mavlink


class _NullFile:
    def write(self, data: bytes) -> int:  # noqa: D401
        return len(data)

    def read(self, _n: int) -> bytes:
        return b""


def derive_key(passphrase: str) -> bytes:
    """운영 비밀문구 → 32바이트 서명키(SHA-256). 실배치의 사전공유키에 해당."""
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def verify_signature(raw_frame: bytes, key: bytes) -> bool:
    """단일 MAVLink 프레임의 서명 인증성만 검사(재생창 무시).

    올바른 키로 서명된 프레임만 True. 미서명·위조서명·타 키 서명은 모두 False.
    프레임마다 새 파서를 써 링크 상태 오염을 피한다.
    """
    v = _mavlink.MAVLink(_NullFile(), srcSystem=0, srcComponent=0)
    v.robust_parsing = True
    v.signing.secret_key = key
    v.signing.timestamp = 0                                   # 인증성만: 재생검사는 상위에서
    v.signing.allow_unsigned_callback = lambda _m, _mid: False  # 미서명 거부
    try:
        msgs = v.parse_buffer(raw_frame) or []
    except Exception:  # noqa: BLE001
        return False
    return any(m.get_type() != "BAD_DATA" for m in msgs)


def signature_metadata(raw_frame: bytes) -> Optional[tuple[int, int]]:
    """MAVLink2 signed frame의 ``(link_id, 48bit timestamp)``를 반환한다.

    서명 trailer는 link_id 1B + timestamp 6B little-endian + signature 6B다.
    프레임 형식이나 signed incompat flag가 맞지 않으면 None을 반환한다.
    """
    if len(raw_frame) < 25 or raw_frame[0] != 0xFD:  # MAVLink2 magic + 최소 signed frame
        return None
    incompat_flags = raw_frame[2]
    if not (incompat_flags & 0x01) or len(raw_frame) < 13:
        return None
    link_id = raw_frame[-13]
    timestamp = int.from_bytes(raw_frame[-12:-6], "little")
    return link_id, timestamp


def check_replay_window(last_seen: dict[tuple[int, int, int], int],
                        stream: tuple[int, int, int], timestamp: int,
                        now_2015: int) -> str:
    """서명 stream timestamp를 갱신하고 ``ok|replay|stale_timestamp``를 반환."""
    previous = last_seen.get(stream)
    if previous is not None and timestamp <= previous:
        return "replay"
    if timestamp + 6_000_000 < now_2015:  # MAVLink 권고 1분 window, 10us tick
        return "stale_timestamp"
    last_seen[stream] = timestamp
    return "ok"
