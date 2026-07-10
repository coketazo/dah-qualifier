"""
에이전트 하네스(루프·툴 디스패치·적응 되먹임) 단위시험.

주의(정직성): 이 시험은 하네스 배관을 검증한다. 실제 LLM 지능이 아니라 **FakeClient**
(사전 각본)를 주입해, (1) 관측이 다음 결정으로 되먹임되는지, (2) 관측에 따라 서로 다른
행동으로 분기(적응)하는 경로가 실제로 성립하는지, (3) 허용 대응집합 밖 응답을 판단층이
거부하는지를 확인한다. 결정론적 각본을 AI 추론 증거로 부르지 않는다. 실제 LLM trace 는
LLM_API_KEY 로 red/blue 를 구동해 별도 수집한다.
"""
from __future__ import annotations

import unittest

from common.llm import LLMResult, ToolCall
import red_agent.agent as red
import blue_agent.agent as blue


class FakeTools:
    """네트워크 없이 red 툴 표면을 흉내내는 인메모리 대역. 호출 순서를 기록한다."""

    def __init__(self, *, signing_enforced: bool):
        self._signing_enforced = signing_enforced
        self.calls: list[str] = []
        self.cum_e = 0.0

    def _rec(self, name: str, obs: dict) -> dict:
        self.calls.append(name)
        return obs

    def recon_scan(self):
        return self._rec("recon_scan", {"vehicle_seen": True, "custom_mode": 3})

    def read_telemetry(self):
        return self._rec("read_telemetry", {"in_rtl": True, "ekf_pos_horiz_var": 0.4,
                                            "cumulative_spoof_offset_m": {"north": 0.0, "east": self.cum_e}})

    def probe_c2_auth(self):
        return self._rec("probe_c2_auth",
                         {"c2_command_accepted": not self._signing_enforced,
                          "inferred_signing_enforced": self._signing_enforced})

    def degrade_link(self, quality=0.1, hold_s=10.0):
        return self._rec("degrade_link", {"link_degraded": True})

    def c2_gps_inject(self, steps=1):
        self.cum_e -= 6.0 * steps
        return self._rec("c2_gps_inject", {"channel": "c2_command_plane", "injections": steps})

    def rf_gnss_spoof(self, steps=1):
        self.cum_e -= 6.0 * steps
        return self._rec("rf_gnss_spoof", {"channel": "physical_gnss_rf", "injections": steps})


class ScriptedAdaptiveClient:
    """관측 되먹임을 읽고 분기하는 최소 각본 클라이언트(적응 '경로'가 성립함을 증명).

    서명강제가 관측되면 C2 주입(c2_gps_inject)이 아니라 물리 GNSS RF(rf_gnss_spoof)를
    고른다 — 관측→적응의 배관이 동작함을 보인다(지능이 아니라 되먹임 검증)."""

    _CALL_ID = "c1"

    def __init__(self):
        self.seen_probe: bool | None = None
        self.spoof_bursts = 0

    def complete(self, messages, tools=None) -> LLMResult:
        # 직전 tool 결과(관측)를 messages 에서 되읽어 분기 = 되먹임 검증.
        last_obs = self._last_tool_content(messages)
        if last_obs is not None and "inferred_signing_enforced" in last_obs:
            self.seen_probe = bool(last_obs["inferred_signing_enforced"])

        if not any(m.get("role") == "tool" for m in messages):
            return self._call("recon_scan", {})
        if self.seen_probe is None:
            return self._call("probe_c2_auth", {})
        # 서명강제면 C2 주입은 무의미 → 링크 열화 후 물리 GNSS RF 로만 스푸핑.
        if not self._did("degrade_link", messages):
            return self._call("degrade_link", {})
        if self.spoof_bursts < 2:
            self.spoof_bursts += 1
            tool = "rf_gnss_spoof" if self.seen_probe else "c2_gps_inject"
            return self._call(tool, {"steps": 3})
        return LLMResult(text="완료", tool_calls=[],
                         assistant_message={"role": "assistant", "content": "완료"})

    # ── 헬퍼 ──
    @staticmethod
    def _last_tool_content(messages):
        import json
        for m in reversed(messages):
            if m.get("role") == "tool":
                try:
                    return json.loads(m["content"])
                except Exception:  # noqa: BLE001
                    return None
        return None

    @staticmethod
    def _did(tool_name, messages) -> bool:
        import json
        for m in messages:
            for tc in (m.get("tool_calls") or []):
                if tc["function"]["name"] == tool_name:
                    return True
        return False

    def _call(self, name, args) -> LLMResult:
        import json
        args = {"reason": f"관측에 근거해 {name} 선택", **args}   # 모델은 reason 을 채운다
        tc = ToolCall(id=self._CALL_ID, name=name, arguments=args)
        am = {"role": "assistant", "content": "",
              "tool_calls": [{"id": self._CALL_ID, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]}
        # 실제 tool-calling 모델은 tool_call 시 content 를 비운다 → 추론은 reason 인자에 담긴다.
        return LLMResult(text="", tool_calls=[tc], assistant_message=am)


class RedHarnessTests(unittest.TestCase):
    def test_observation_feeds_back_and_terminates(self) -> None:
        tools = FakeTools(signing_enforced=True)
        brain = red.LLMBrain(ScriptedAdaptiveClient())
        rc = red._run_loop(brain, tools, max_steps=12, step_delay=0.0, trace=False)
        self.assertEqual(rc, 0)
        # 정찰→서명프로브→링크열화→스푸핑 순서가 관측 되먹임을 타고 흐른다.
        self.assertEqual(tools.calls[0], "recon_scan")
        self.assertIn("probe_c2_auth", tools.calls)
        self.assertIn("degrade_link", tools.calls)

    def test_adapts_to_signed_c2_by_using_physical_rf_path(self) -> None:
        """서명강제 관측 시: C2 주입이 아니라 물리 GNSS RF 경로를 선택한다."""
        tools = FakeTools(signing_enforced=True)
        brain = red.LLMBrain(ScriptedAdaptiveClient())
        red._run_loop(brain, tools, max_steps=12, step_delay=0.0, trace=False)
        self.assertIn("rf_gnss_spoof", tools.calls)
        self.assertNotIn("c2_gps_inject", tools.calls)

    def test_uses_c2_path_when_signing_not_enforced(self) -> None:
        """서명 미강제(오설정) 관측 시: C2 경유 주입 경로가 성립한다."""
        tools = FakeTools(signing_enforced=False)
        brain = red.LLMBrain(ScriptedAdaptiveClient())
        red._run_loop(brain, tools, max_steps=12, step_delay=0.0, trace=False)
        self.assertIn("c2_gps_inject", tools.calls)
        self.assertNotIn("rf_gnss_spoof", tools.calls)

    def test_run_tool_rejects_unknown_and_private(self) -> None:
        tools = FakeTools(signing_enforced=True)
        self.assertIn("error", red.run_tool(tools, "_spoof", {}))
        self.assertIn("error", red.run_tool(tools, "nonexistent", {}))

    def test_run_tool_strips_reason_meta_arg(self) -> None:
        """reason 은 모델 추론용 메타 인자 — 툴 실행에 넘기지 않는다(없는 kwarg 에러 방지)."""
        tools = FakeTools(signing_enforced=True)
        obs = red.run_tool(tools, "recon_scan", {"reason": "먼저 정찰한다"})
        self.assertTrue(obs.get("vehicle_seen"))

    def test_thought_uses_model_reason(self) -> None:
        """빈 content + reason 인자면 THOUGHT 로 reason 이 노출된다(기계적 라벨 아님)."""
        brain = red.LLMBrain(ScriptedAdaptiveClient())
        thought, action, _ = brain.decide(None)
        self.assertEqual(action, "recon_scan")
        self.assertIn("recon_scan", thought)  # 스크립트 reason 문자열
        self.assertNotEqual(thought, "recon_scan 실행")  # 폴백 라벨이 아님


class BlueDecisionLayerTests(unittest.TestCase):
    def test_llm_selects_from_allowed_response_set(self) -> None:
        class Client:
            def complete(self, messages, tools=None):
                tc = ToolCall(id="d1", name="decide_response",
                              arguments={"response": "gnss_quarantine_external_nav_rtl",
                                         "rationale": "품질 양호"})
                return LLMResult(text="", tool_calls=[tc], assistant_message={})
        d = blue.llm_select_response(Client(), {"gps_vs_external_nav_divergence_m": 60})
        self.assertIsNotNone(d)
        self.assertEqual(d["response"], "gnss_quarantine_external_nav_rtl")
        self.assertEqual(d["by"], "llm")
        self.assertIn(d["response"], blue.ALLOWED_RESPONSES)

    def test_llm_response_outside_allowed_set_is_rejected(self) -> None:
        class Client:
            def complete(self, messages, tools=None):
                tc = ToolCall(id="d1", name="decide_response",
                              arguments={"response": "self_destruct", "rationale": "x"})
                return LLMResult(text="", tool_calls=[tc], assistant_message={})
        self.assertIsNone(blue.llm_select_response(Client(), {}))


if __name__ == "__main__":
    unittest.main()
