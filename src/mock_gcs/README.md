# mock_gcs — 전술급 회전익 UAV 임무시스템 축소 표적

이름은 역사적으로 `mock_gcs`지만 실제 구성은 **오토파일럿 + C2 링크 + 센서 환경 + 운영 대시보드**다. 실제 ArduPilot 자체가 아니라, Green-Board Hijack의 신뢰경계와 제어 오유도를 재현하는 축소차수 표적이다.

## 실행

```bash
cd src
SECURE=true ../.venv/bin/uvicorn mock_gcs.app:app --port 8137
```

- C2 공격면: `udp:127.0.0.1:14550`
- GNSS 센서 모사: `udp:127.0.0.1:14600` — localhost 시험전용, 외부 노출 금지
- 내부 REST/대시보드: `http://127.0.0.1:8137`

환경변수: `SECURE`, `MAV_HOST`, `MAV_PORT`, `SENSOR_HOST`, `SENSOR_MAV_PORT`, `MAV_OPER_KEY`, `LOG_PATH`.

## C2 정책

`SECURE=true`에서는 `RADIO_STATUS` 예외를 제외한 미서명·위조서명 인바운드 프레임을 종류와 무관하게 거부한다. 따라서 같은 C2 링크의 미서명 `GPS_INPUT`도 처리하지 않는다.

`SECURE=false`는 레거시/오구성 MAVLink 배치를 재현하며 미서명 명령과 C2 `GPS_INPUT`을 수용할 수 있다.

## 물리 GNSS 공격 모사

센서 포트의 `GPS_INPUT`은 C2 공격 메시지가 아니다. GNSS RF simulator가 생성한 오염 측정을 오토파일럿으로 전달하는 HIL 어댑터다. 이벤트에는 다음 `trust_domain`이 기록된다.

- `c2_insecure`: 무인증 C2 구성오류를 통한 센서주입
- `gnss_rf_emulator`: 물리 GNSS RF 환경 모사
- `c2`: secure C2에서 거부된 프레임

## 항법·방어 상태

- 주 항법: GNSS를 융합하는 재귀 EKF
- 독립 보조항법: VIO/지형대조를 대표하는 bounded-error ExternalNav
- 명시 메시지: `ODOMETRY(estimator_type=VIO, quality=90)`
- v3 호환: 같은 ExternalNav를 `LOCAL_POSITION_NED`로도 송출하지만 메시지 자체를 독립 INS라고 주장하지 않음
- 복구: `MONITORING → GNSS_QUARANTINED → EXTERNAL_NAV_RTL`

## 판정

- `estimate_true_bias_m`: EKF 추정과 실제 위치 차이
- `mission_compromised`: 편이 100m 이상이며 방어되지 않은 상태
- `platform_availability`: 비행제어·관측 서비스
- `c2_availability`: 데이터링크 품질

과거 `hijacked`, `availability`, `ins_position`은 호환 별칭이다.

## 표적 수용시험

```bash
cd src
../.venv/bin/python -m demo.run_target_scenarios
../.venv/bin/python -m unittest discover -s tests -v
```

첫 명령은 C2 서명과 물리 센서 공격의 분리, 무방어 임무 손실, Blue 복구를 검증한다. 두 번째는 30분 상당 정상운용·성공지표·복구상태·ODOMETRY 계약을 검증한다. 둘 다 AI 에이전트 평가가 아닌 표적 acceptance test다.

## REST

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/api/status` | C2/센서 endpoint·secure 상태 |
| GET | `/api/truth` | ground truth·두 가용성·임무 무결성 |
| GET | `/api/events?n=` | 감사로그 |
| POST | `/api/_env/link_degrade` | RF 링크 환경효과 |
| POST | `/api/defense/mitigate` | GNSS 격리·ExternalNav 복구 시작 |
| POST | `/api/reset` | 초기화 |

REST는 실제 공격면이 아니라 시험통제 평면이다. 실제 배치에서 완화 훅은 signed MAVLink EKF source command, onboard Lua 또는 companion policy module로 대체한다.
