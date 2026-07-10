# DAH 2026 — 프로젝트 컨텍스트 (에이전트/팀 공용)

> DAH 2026(국방 AI 사이버보안 해커톤, LIG 후원) **예선** 부가자료.
> 마감: **2026-07-10 23:59 KST**. 팀: coketazo(트랙 A 담당) · sy(트랙 B 담당) · Claude(스캐폴딩·문서).
> 팀 배경: 웹개발자 2인, 공방해킹·에이전트 경험 전무 → Claude가 실행 시스템을 구축.

## 무엇을 만들었나 (한 줄)
AI **공격 에이전트(red)** 와 **방어 에이전트(blue)** 가 자체 제작한 **전술급 회전익 UAV 임무시스템 축소 표적(mock_gcs)** 에서 표준 MAVLink 2.0으로 공방한다. 시나리오 = **Green-Board Hijack**.

## 시나리오 논지 (핵심)
링크 열화로 **정상 페일세이프(RTL)** 를 유도 → signed C2와 별개의 **물리 GNSS 신뢰영역**을 slow-takeover → 예상 가능한 RTL 상태 뒤에서 위치추정과 실제 항적을 누적 이탈시킨다. Blue는 `GPS_RAW_INT` ↔ 출처가 명시된 독립 `ODOMETRY(ExternalNav)` 발산을 탐지해 **GNSS 격리→안전 LOITER→ExternalNav RTL**로 복구한다. 플랫폼 가용성·C2 가용성·임무 무결성은 별도 지표다.

## 아키텍처 (실제 파일)
```
src/
  mavproto/    # 공용 프로토콜: dialect(ardupilotmega·노드ID) · signing(MAVLink2 HMAC) · link(UDP 멀티클라이언트)
  common/      # wire.py(값객체·어휘) · geo.py(지리) · llm.py(provider-agnostic LLM 어댑터) · policy.py(방어정책, red 미공유)
  mock_gcs/    # autopilot.py(EKF·ExternalNav·복구 SM) + mav_server.py(C2/센서 경계) + app.py
  red_agent/   # LLM-only 자율 공격(C2 vs 물리 GNSS RF 툴 분리·관측기반 적응). HeuristicBrain 제거됨
  blue_agent/  # LLM 판단층 + 규칙 검증도구. 독립출처=ODOMETRY(VIO). 대응선택 trace
  demo/        # run_target_scenarios.py(표적 수용시험) + run_agent_engagement.py(LLM 교전·§6 증거)
  tests/       # 정상 장기운용·복구·계약 회귀 + 에이전트 하네스(관측 되먹임·적응 분기)
docs/          # 00 예선보고서 · 02 계약 v4 · 03 시나리오 · 04 도메인 신뢰경계 · 01 학습노트
```

### 핵심 메커니즘
- **C2 공격 표면 = MAVLink UDP `:14550`**. secure이면 `RADIO_STATUS` 외 미인증 인바운드 전부 거부.
- **센서 모사 = localhost UDP `:14600`**. 물리 GNSS RF 결과를 `GPS_INPUT`으로 변환하는 HIL 어댑터이며 C2 우회로가 아님.
- **REST `:8137` = 내부 시험통제**. ground truth·환경·완화·dashboard 전용.
- **재귀 EKF**: 12m 순간 혁신 게이트. 임무 손실 판정은 별도 100m 편이.
- **독립 출처**: `ODOMETRY(estimator_type=VIO)` 정본. `LOCAL_POSITION_NED`는 기존 연동 호환용.
- **노드ID**: 기체 1/1, 오퍼레이터 255/190, red 255/190, blue 254/191, GNSS emulator 42/220.

## 잠근 결정 (변경 시 여기 갱신)
1. **최종 에이전트는 LLM-only**. HeuristicBrain 폴백은 제거됨(키 없으면 red 는 실행 중단). 결정론적 러너는 agent가 아니라 acceptance test로만 사용.
2. **정본 공격 = 링크열화→RTL→물리 GNSS slow-takeover**. `GPS_INPUT`은 C2 공격과 센서 모사 경로를 구분한다.
3. **본선 오케스트레이션 = 자체 하네스**(LangGraph 아님) — 설명가능성·에어갭·공급망 최소화·팀역량. 보고서에 "LangGraph 검토했으나 자체 채택" 명시.
4. **Blue 규칙은 LLM의 검증도구**로 사용하며 최종 판단·대응 선택 trace는 LLM-only 구조에서 제시.
5. **에어갭/Hermes = §7(향후)**, §6에 훅만(교체형 Brain·규칙레인은 지금도 외부호출 0으로 에어갭 동작).

## 실행 / 검증
```bash
cd src && ../.venv/bin/python -m demo.run_target_scenarios          # 결정론적 표적 수용시험(AI 아님)
cd src && ../.venv/bin/python -m unittest discover -s tests -v      # 13개
cd src && SECURE=true ../.venv/bin/uvicorn mock_gcs.app:app --port 8137
cd src && LLM_API_KEY=gsk_... ../.venv/bin/python -m demo.run_agent_engagement [--secure]  # LLM 교전(§6 trace, 키 필요)
```
**표적 수용결과**: T1 무방어→임무손실·약 225m / T2 Blue→ExternalNav 복구·1.6m / T3 Signed C2+Blue→미인증 C2 3건·C2 센서주입 1건 거부 + 물리 센서 공격 복구·1.7m. 단위시험 **13개** 통과.
**LLM 교전(키 필요)**: red가 서명강제 관측 시 `c2_gps_inject`→`rf_gnss_spoof`(물리 GNSS RF)로 적응. 교전 배관은 결정론 각본으로 독립 검증됨(secure 무방어 300m대 손실 / blue 방어 2m 이하).

## 그레이박스 원칙 (필수)
- **red_agent 는 `common.policy` 를 import 금지.** `mavproto`(공개규약)·`common.geo/wire/llm` 만.
- red가 아는 것: MAVLink 메시지·COMMAND_ACK·텔레메트리. 모르는 것: 서명키·탐지 임계값·완화 알고리즘.

## 채점 (예선)
공격시나리오 30 / 방어전략 25 / AI에이전트 25 / 팀역량 10 / 문서완성도 10. 제공된 예선 PDF에는 본선 가용성 산식이 없으므로 별도 공식 출처 없이는 주장 금지.

## 현황 & 남은 일
- ✅ C2/센서 신뢰경계 분리, ExternalNav 복구 상태기, 정직한 성공지표, 표적 수용시험·정상 장기시험 구현.
- ✅ red/blue LLM-only 마이그레이션(HeuristicBrain 제거, C2/물리RF 툴 분리, blue 판단층+ODOMETRY, 교전 하네스, 하네스 단위시험).
- ✅ **예선 보고서** 초안(`docs/00_예선보고서.md`, §1~§8 + 부록).
- ⬜ 팀정보(§3) 실명·연락처, 표지 서식 확정.
- ⬜ (키 확보 시) `run_agent_engagement`로 실제 LLM 추론 trace 수집해 §6.5에 삽입.
- ⬜ sy §6 문서(Downloads/message.txt)를 실코드에 맞춰 갱신(툴표→MAVLink, common/llm.py 실재 반영).

## 정직성 메모
표적은 축소차수 모델이다. MAVLink wire format·HMAC 서명·stream별 anti-replay·GPS_INPUT·ODOMETRY·heartbeat failsafe·재귀 추정/제어 coupling은 실행하고, RF 파형·공기역학·실 ArduPilot EKF·센서 성능·키 배포/회전 운영은 축소한다. 결정론적 수용시험을 AI 추론으로 부르지 않는다.
