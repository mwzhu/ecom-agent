from __future__ import annotations

import agents.order_exception.llm_layer as llm_layer


def test_subagent_refinement_keeps_tool_plan_and_approval_fields_locked(monkeypatch) -> None:
    def fake_invoke_json(**_: object) -> dict[str, object]:
        return {
            "summary": "Refined summary.",
            "recommendation": "Refined recommendation.",
            "confidence": 0.91,
            "rationale": ["The requested item edit is concrete and still pre-shipment."],
            "requires_human": False,
            "tool_calls": [{"tool": "tampered"}],
            "matched_fop_ids": [],
        }

    monkeypatch.setenv("ORDER_EXCEPTION_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(llm_layer, "_invoke_json", fake_invoke_json)
    proposed = {
        "type": "item_change_request",
        "summary": "Base summary.",
        "recommendation": "Base recommendation.",
        "requires_human": True,
        "required_approvals": ["order_edit"],
        "tool_calls": [{"tool": "shopify_apply_order_edit"}],
        "matched_fop_ids": ["fop_item_change_zero_delta"],
        "hard_constraints": ["Draft before write."],
        "confidence": 0.83,
        "rationale": ["Base rationale."],
    }

    refined = llm_layer.refine_subagent_proposal(
        state={"exception_type": "item_change_request"},
        proposed_action=proposed,
    )

    assert refined["summary"] == "Refined summary."
    assert refined["recommendation"] == "Refined recommendation."
    assert refined["confidence"] == 0.91
    assert refined["requires_human"] is True
    assert refined["tool_calls"] == [{"tool": "shopify_apply_order_edit"}]
    assert refined["matched_fop_ids"] == ["fop_item_change_zero_delta"]
