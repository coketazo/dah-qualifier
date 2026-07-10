"""v4 호환 진입점.

과거 이 모듈은 미서명 C2 GPS_INPUT을 secure 배치에서도 센서 공격처럼 취급했다.
v4에서는 C2와 물리 GNSS 신뢰영역을 분리했으므로 표적 수용시험 정본으로 위임한다.

AI 에이전트 통합 데모는 LLM-only 마이그레이션 후 별도로 갱신한다.
"""
from demo.run_target_scenarios import main


if __name__ == "__main__":
    main()
