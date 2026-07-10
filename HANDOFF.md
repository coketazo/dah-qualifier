# DAH2026 진행 공유 (2026-07-10)

**리포:** https://github.com/coketazo/dah-qualifier (`main` 브랜치)

```bash
git clone https://github.com/coketazo/dah-qualifier.git
cd dah-qualifier && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

## ✅ 이번에 끝난 것

1. **git 저장소 초기화 + GitHub 푸시** (백업 tarball도 있음)
2. **red/blue 에이전트 LLM-only 재구현** (AI 배점 25점)
   - red: 규칙기반 폴백 제거, `C2 신뢰영역(c2_gps_inject)` vs `물리 GNSS RF(rf_gnss_spoof)` 툴 분리. **서명강제를 관측하면 물리 RF로 적응**. 그레이박스 유지(policy 미접근).
   - blue: 독립출처를 `LOCAL_POSITION_NED` → **`ODOMETRY(VIO)`** 로 전환. 규칙=검증도구, LLM이 대응 선택+근거 trace 기록.
   - 교전 하네스 `demo/run_agent_engagement.py` 추가.
3. **T1 센서 타임아웃 버그 수정** (마지막 프레임 스냅백 아티팩트 제거)
4. **예선 보고서 초안** `docs/00_예선보고서.md` (§1~§8 + 부록)
5. **상세 README** (아키텍처/킬체인/복구 다이어그램)

## 검증 (모두 통과)

```bash
cd src && ../.venv/bin/python -m demo.run_target_scenarios   # T1 225m / T2 1.6m / T3 1.7m
cd src && ../.venv/bin/python -m unittest discover -s tests  # 13개 OK
```

## ⬜ 우리가 마저 해야 할 것

| 담당 | 할 일 |
|---|---|
| **팀 전체** | 보고서 **§3 팀정보**(실명·역할·연락처)와 표지 서식 채우기 |
| **sy** | 무료 **Groq 키**로 아래 실행해서 실제 LLM trace 뽑아 §6.5에 삽입 |
| **sy** | 예전 §6 문서(`~/Downloads/message.txt`)를 실제 코드에 맞게 갱신 |

```bash
# Groq 무료 키 발급 후 (console.groq.com)
cd src && LLM_API_KEY=gsk_xxx ../.venv/bin/python -m demo.run_agent_engagement --secure
```

## ⚠️ 확인 필요

- **리포가 현재 public임.** 심사 전까지 비공개로 둘지 팀 결정 필요
  (`gh repo edit coketazo/dah-qualifier --visibility private`)

## 핵심 논지 (보고서·발표용 한 줄)

> 링크 열화로 **정상 RTL 페일세이프**를 유도한 뒤, **서명된 C2와 별개인 물리 GNSS RF 신뢰영역**을 slow-takeover. "서명은 만능/무용" 둘 다 과장 안 함 → **C2 인증과 센서 진실성은 별도 신뢰경계, 다층 방어 필요.** blue는 GNSS↔ODOMETRY **누적 발산**으로 탐지·복구.
