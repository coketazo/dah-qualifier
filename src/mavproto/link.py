"""
UDP MAVLink 링크 계층.

- MavServer   : 표적(기체측) 엔드포인트. 하나의 UDP 소켓에 다수 클라이언트
                (오퍼레이터 GCS·red·blue)가 붙는다. 수신 패킷에서 주소를 학습해
                텔레메트리를 전 클라이언트로 전파(브로드캐스트 관제 모사).
- connect_agent: red/blue 가 표적에 붙는 mavutil 커넥션 헬퍼(서명 설정 포함).

송신 파서(TX)는 서명하고, 수신 파서(RX)는 키 없이 파싱(정상 프레임 드롭 방지).
프레임 인증성은 signing.verify_signature() 로 프레임 단위 독립 검증한다.
"""
from __future__ import annotations

import socket
import time
from typing import NamedTuple, Optional

from pymavlink import mavutil

from .dialect import mavlink, MAVLINK_IFLAG_SIGNED, MavId
from .signing import verify_signature, signature_metadata, check_replay_window


class RxMsg(NamedTuple):
    msg: object          # pymavlink 메시지 객체
    addr: tuple          # 송신자 UDP 주소
    signed: bool         # MAVLink2 서명 프레임 여부(헤더 incompat_flag)
    authentic: bool      # 공유키로 서명 인증 통과 여부(위조서명은 False)
    auth_reason: str     # ok | unsigned | invalid_signature | replay | stale_timestamp


class _Sink:
    """MAVLink.pack() 이 쓰는 파일 유사 객체. 실제 송신은 MavServer 가 담당."""
    def write(self, data: bytes) -> int:  # noqa: D401
        return len(data)

    def read(self, _n: int) -> bytes:
        return b""


def _new_mav(me: MavId):
    return mavlink.MAVLink(_Sink(), srcSystem=me.system, srcComponent=me.component)


class MavServer:
    """표적측 다중 클라이언트 MAVLink UDP 엔드포인트."""

    def __init__(self, bind_host: str = "127.0.0.1", bind_port: int = 14550, *,
                 me: Optional[MavId] = None,
                 secret_key: Optional[bytes] = None,
                 require_signing: bool = False,
                 client_ttl_s: float = 15.0):
        me = me or MavId(1, mavlink.MAV_COMP_ID_AUTOPILOT1, "autopilot")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_host, bind_port))
        self.sock.setblocking(False)

        # TX: 발신 텔레메트리 서명(기체 자기 인증). RX: 키 없이 파싱(무드롭).
        self._tx = _new_mav(me)
        self._rx = _new_mav(me)
        self._rx.robust_parsing = True
        self.require_signing = require_signing
        self.secret_key = secret_key
        if secret_key:
            self._tx.signing.secret_key = secret_key
            self._tx.signing.sign_outgoing = True
            self._tx.signing.link_id = 0
            self._tx.signing.timestamp = int((max(time.time(), 1420070400) - 1420070400) * 100_000)

        self._clients: dict[tuple, float] = {}
        self._last_signature_ts: dict[tuple[int, int, int], int] = {}
        self._ttl = client_ttl_s

    # ── 송신: 학습된 모든 클라이언트로 전파 ──
    def broadcast(self, msg) -> None:
        buf = msg.pack(self._tx)
        now = time.monotonic()
        for addr, seen in list(self._clients.items()):
            if now - seen > self._ttl:
                self._clients.pop(addr, None)
                continue
            try:
                self.sock.sendto(buf, addr)
            except OSError:
                pass

    # ── 수신: 주소 학습 + 파싱 + 프레임 인증성 검증 ──
    def poll(self, max_pkts: int = 128) -> list[RxMsg]:
        out: list[RxMsg] = []
        for _ in range(max_pkts):
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, OSError):
                break
            self._clients[addr] = time.monotonic()
            try:
                msgs = self._rx.parse_buffer(data) or []
            except Exception:  # noqa: BLE001  잘못된 프레임 무시(robust)
                continue
            for msg in msgs:
                if msg.get_type() == "BAD_DATA":
                    continue
                try:
                    signed = bool(msg.get_header().incompat_flags & MAVLINK_IFLAG_SIGNED)
                except Exception:  # noqa: BLE001
                    signed = False
                authentic = False
                auth_reason = "unsigned"
                if signed and self.secret_key is not None:
                    authentic = verify_signature(msg.get_msgbuf(), self.secret_key)
                    auth_reason = "ok" if authentic else "invalid_signature"
                    meta = signature_metadata(msg.get_msgbuf()) if authentic else None
                    if meta is not None:
                        link_id, signature_ts = meta
                        stream = (msg.get_srcSystem(), msg.get_srcComponent(), link_id)
                        now_2015 = int((max(time.time(), 1420070400) - 1420070400) * 100_000)
                        auth_reason = check_replay_window(
                            self._last_signature_ts, stream, signature_ts, now_2015)
                        authentic = auth_reason == "ok"
                out.append(RxMsg(msg, addr, signed, authentic, auth_reason))
        return out

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def connect_agent(host: str = "127.0.0.1", port: int = 14550, *,
                  me: MavId, secret_key: Optional[bytes] = None,
                  sign_outgoing: bool = False):
    """red/blue → 표적 접속. udpout: 첫 패킷으로 서버가 이 클라이언트를 학습한다."""
    conn = mavutil.mavlink_connection(
        f"udpout:{host}:{port}", dialect="ardupilotmega",
        source_system=me.system, source_component=me.component)
    if secret_key:
        conn.setup_signing(secret_key, sign_outgoing=sign_outgoing)
    return conn
