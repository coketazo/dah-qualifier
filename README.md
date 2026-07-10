# DAH 2026 예선 부가자료 — Green-Board Hijack

**팀:** (팀명 입력 필요) · **표적:** 전술급 회전익 ISR UAV 임무시스템 축소 모사

링크 열화로 정상 페일세이프 RTL을 유도한 뒤, **서명된 C2와 별개의 GNSS 센서 신뢰영역**을 점진 오염하는 공격과 이를 독립 ExternalNav 교차정합으로 탐지·복구하는 시스템이다. 표준 MAVLink 2.0 프레임·서명·`GPS_INPUT`·`ODOMETRY`를 사용하지만, 비행역학과 EKF는 재현성을 위한 축소차수 모델이다.

## 가장 먼저 실행할 것

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 표적 신뢰경계 수용시험: AI 에이전트와 분리된 결정론적 T1/T2/T3
cd src && ../.venv/bin/python -m demo.run_target_scenarios

# 단위시험: 정상 30분 상당·성공지표·복구상태·ODOMETRY 계약
cd src && ../.venv/bin/python -m unittest discover -s tests -v

# 대시보드/표적 서버
cd src && SECURE=true ../.venv/bin/uvicorn mock_gcs.app:app --port 8137
```

최신 표적 수용 결과:

| 시험 | 결과 |
|---|---|
| T1 무방어 | 임무 무결성 상실, 편이 약 220m |
| T2 Blue | GNSS 격리·ExternalNav 복구, 최종 편이 2m 이하 |
| T3 Signed C2 + Blue | 미인증 C2 3건·C2 센서주입 1건 거부, 물리 센서 공격 별도 탐지, 최종 편이 2m 이하 |

## 신뢰경계

| 평면 | 기본 주소 | 역할 |
|---|---|---|
| C2 MAVLink | `udp:127.0.0.1:14550` | 실제 공격·관측 표면. secure이면 미인증 인바운드 거부 |
| GNSS 센서 모사 | `udp:127.0.0.1:14600` | 물리 GNSS RF 공격을 `GPS_INPUT`으로 변환하는 localhost 시험 인터페이스 |
| 내부 REST | `http://127.0.0.1:8137` | ground truth·환경·완화·대시보드. 공격면 아님 |

`GPS_INPUT`을 같은 signed C2에서 몰래 통과시켜 “서명은 센서를 못 막는다”고 주장하지 않는다. signed C2는 미인증 메시지를 차단하고, GNSS RF 공격은 별도 물리 신뢰영역에서 발생한다.

## 구조

```text
src/
  mavproto/      MAVLink2 다이얼렉트·서명·UDP 링크
  common/        값객체·지리·LLM adapter·방어정책
  mock_gcs/      C2/센서 경계 + 축소 오토파일럿 + 대시보드
  red_agent/     LLM-only 공격 에이전트(C2 vs 물리 GNSS RF 툴 분리·관측기반 적응)
  blue_agent/    LLM 판단층 + 규칙 검증도구(독립출처 ODOMETRY·대응선택 trace)
  demo/          표적 수용시험(run_target_scenarios) · LLM 교전(run_agent_engagement)
  tests/         표적 모델 회귀시험 + 에이전트 하네스 시험(13개)
docs/
  00_예선보고서.md
  02_인터페이스-계약.md
  03_시나리오_그린보드-하이재킹.md
  04_도메인-아키텍처_신뢰경계.md
```

## 구현된 표적 메커니즘

- GCS heartbeat 상실 → RTL 페일세이프 상태전이
- 재귀 EKF와 12m 순간 혁신 게이트
- GNSS slow-takeover가 실제 `true_position` 운동을 오유도
- 독립 ExternalNav를 `ODOMETRY(estimator_type=VIO)`로 명시
- `MONITORING → GNSS_QUARANTINED → EXTERNAL_NAV_RTL` 복구 상태기
- 추정–실제 편이 100m 이상을 임무 무결성 상실로 판정
- 플랫폼 가용성과 C2 링크 가용성 분리
- 이벤트별 `trust_domain` 감사로그

상세 시나리오: [docs/03_시나리오_그린보드-하이재킹.md](docs/03_시나리오_그린보드-하이재킹.md)  
도메인 아키텍처: [docs/04_도메인-아키텍처_신뢰경계.md](docs/04_도메인-아키텍처_신뢰경계.md)  
프로토콜 계약: [docs/02_인터페이스-계약.md](docs/02_인터페이스-계약.md)

## AI 에이전트 증거와 수용시험의 구분

`demo.run_target_scenarios`는 표적과 신뢰경계가 맞는지 검증하는 **결정론적 시험 하네스**이며 AI 에이전트가 아니다. AI 증거는 LLM-only red/blue가 관측에 따라 tool을 선택·수정·중단하는 trace로, `demo.run_agent_engagement`(LLM_API_KEY 필요)를 구동해 별도 수집한다. red 는 서명강제를 관측하면 C2 경유 주입을 포기하고 물리 GNSS RF 경로로 적응한다. 결정론적 각본 로그를 AI 판단 증거로 사용하지 않는다.

```bash
cd src && ../.venv/bin/python -m demo.run_target_scenarios          # 결정론적 수용시험
cd src && ../.venv/bin/python -m unittest discover -s tests         # 13개
cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m demo.run_agent_engagement [--secure]
```

## 정직성 범위

- 실제: MAVLink 2.0 wire format, 메시지 서명 구조, UDP, `COMMAND_LONG`, `GPS_INPUT`, `ODOMETRY`, heartbeat failsafe, 재귀 추정과 guidance coupling.
- 축소: RF 파형, 안테나·재밍 전력, 공기역학, 실제 ArduPilot EKF, ExternalNav 센서 성능, 키 배포·회전·시계동기화 운영. HMAC과 stream별 timestamp anti-replay는 구현한다.
- 모든 공격은 localhost 자체 mock에서만 수행한다.

## Docker

```bash
docker build -t dah2026 .
docker run -p 8137:8137 -p 14550:14550/udp dah2026
```

센서 모사 포트 `14600/udp`는 의도적으로 외부에 노출하지 않는다.

## 생성형 AI 활용 고지

도메인 학습, 아키텍처 검토, 코드·문서 작성 보조에 생성형 AI를 활용했다. 팀은 위협 모델·신뢰경계·공격 성공조건·방어 복구정책을 검토하고 실행 결과로 검증해야 하며, AI 생성 내용을 팀의 독자 연구로 위장하지 않는다.
