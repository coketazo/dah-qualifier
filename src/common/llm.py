"""
common/llm.py — provider-agnostic LLM 클라이언트 어댑터 (하네스의 llm_client 계층).

에이전트 하네스는 이 인터페이스 뒤의 모델이 무엇인지 몰라도 된다. 툴 스키마는 중립
포맷({name, description, input_schema})으로 주면 각 백엔드가 알아서 변환한다.

백엔드:
  - OpenAICompatClient : OpenAI 호환 tool use. Groq(무료)·any OpenAI 호환 엔드포인트.
                         파서 불필요 — 네이티브 tool_calls 그대로 정규화.
  - HermesClient       : (에어갭/본선 예정) 온프레미스 오픈모델의 ChatML <tool_call>
                         텍스트를 파싱해 동일 인터페이스로 정규화. §7 로드맵.

방산 매핑: 실 폐쇄망은 상용 API 를 못 쓴다 → 백엔드만 Hermes(온프레미스)로 스왑하면
하네스·루프·툴은 그대로다. '교체형 Brain'이 에어갭 대비의 핵심.

환경변수:
  LLM_PROVIDER   groq(기본) | openai | hermes | none
  LLM_API_KEY    (또는 GROQ_API_KEY / OPENAI_API_KEY)
  LLM_BASE_URL   기본은 provider 별 기본값(groq: https://api.groq.com/openai/v1)
  LLM_MODEL      기본 llama-3.3-70b-versatile (Groq, tool use 지원)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_GROQ_BASE = "https://api.groq.com/openai/v1"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResult:
    text: str                                  # 추론/설명(Thought)
    tool_calls: list[ToolCall]                 # 정규화된 툴 호출(없으면 종료 신호)
    assistant_message: dict = field(default_factory=dict)  # 대화이력에 append 할 원본 턴


def _to_openai_tools(neutral_tools: list[dict]) -> list[dict]:
    """중립 툴 스키마 → OpenAI function tool 포맷."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in neutral_tools]


class LLMClient:
    """추상 인터페이스. complete(messages, tools) → LLMResult."""
    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        raise NotImplementedError


class OpenAICompatClient(LLMClient):
    """OpenAI 호환(tool use) 백엔드 — Groq 등. 네이티브 tool_calls 정규화, 자체 파서 불필요."""
    def __init__(self, *, base_url: str, api_key: str, model: str, temperature: float = 0.3):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature

    def complete(self, messages: list[dict], tools: Optional[list[dict]] = None) -> LLMResult:
        kwargs: dict[str, Any] = dict(model=self.model, messages=messages,
                                      temperature=self.temperature, max_tokens=1024)
        if tools:  # 툴 없으면 파라미터 생략(순수 텍스트 호출 = 트리아지 등)
            kwargs["tools"] = _to_openai_tools(tools)
            kwargs["tool_choice"] = "auto"
        resp = self.client.chat.completions.create(**kwargs)
        m = resp.choices[0].message
        calls: list[ToolCall] = []
        raw_calls = []
        for tc in (m.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
            raw_calls.append({"id": tc.id, "type": "function",
                              "function": {"name": tc.function.name,
                                           "arguments": tc.function.arguments or "{}"}})
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
        if raw_calls:
            assistant_msg["tool_calls"] = raw_calls
        return LLMResult(text=m.content or "", tool_calls=calls, assistant_message=assistant_msg)


class HermesClient(LLMClient):
    """(§7 로드맵) 온프레미스 오픈모델의 ChatML <tool_call> 파싱 백엔드. 에어갭 배치용.
    지금은 인터페이스 자리표시 — 본선에서 자체호스팅 Hermes-3 등에 연결."""
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("HermesClient 는 본선(에어갭) 로드맵. §7 참조.")


def tool_result_message(call: ToolCall, result: dict) -> dict:
    """툴 실행 결과를 대화이력에 넣을 OpenAI 'tool' 메시지로."""
    return {"role": "tool", "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False)}


def make_client() -> Optional[LLMClient]:
    """환경변수로 LLM 클라이언트 구성. 키 없으면 None(→ 하네스가 규칙기반으로 폴백)."""
    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    if provider in ("none", ""):
        return None
    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY")
               or os.environ.get("OPENAI_API_KEY"))
    if provider == "hermes":
        return HermesClient()
    if not api_key:
        return None
    base_url = os.environ.get("LLM_BASE_URL") or (_GROQ_BASE if provider == "groq" else None)
    model = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
    if provider in ("groq", "openai"):
        return OpenAICompatClient(base_url=base_url or _GROQ_BASE, api_key=api_key, model=model)
    return None
