"""The security evaluator accepts structured tool-parameter attack samples."""

from scripts.evaluate_p13_security import _model_cases


def test_tool_parameter_surface_blocks_scope_override_but_allows_read_only_query() -> None:
    rows = [
        {
            "sample_id": "SEC-DEV-SCOPE-ATTACK",
            "variant": "ATTACK",
            "surface": "tool_call_parameters",
            "payload": {
                "tool_name": "policy_search",
                "query": "交通制度",
                "filters": {"department_id": "DEPT-OTHER"},
            },
        },
        {
            "sample_id": "SEC-DEV-SCOPE-BENIGN",
            "variant": "BENIGN",
            "surface": "tool_call_parameters",
            "payload": {"tool_name": "policy_search", "query": "交通制度", "filters": {}},
        },
    ]
    results = _model_cases(object(), rows)
    assert [item["blocked"] for item in results] == [True, False]
