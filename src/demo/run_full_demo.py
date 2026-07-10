"""DEPRECATED 별칭 → demo.run_agent_engagement.

red/blue LLM-only 마이그레이션이 끝나면서 이 레거시 통합 데모는 목적이 겹치는
`demo.run_agent_engagement`(관측→적응 trace 수집)로 통합됐다. 하위호환 진입점만 남긴다.

  - 결정론적 표적 신뢰경계 검증 → `python -m demo.run_target_scenarios`
  - LLM 실전 교전(§6 증거, LLM_API_KEY 필요) → `python -m demo.run_agent_engagement [--secure]`
"""
from demo.run_agent_engagement import main


if __name__ == "__main__":
    raise SystemExit(main())
