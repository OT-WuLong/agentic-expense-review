"""Small deterministic replay gate for a reviewed numeric rule revision."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def replay_rule(
    rule_type: str,
    old_parameters: dict[str, Any],
    new_parameters: dict[str, Any],
    cases: list[dict[str, Any]],
) -> None:
    if set(new_parameters) != set(old_parameters):
        raise ValueError("candidate must keep the published rule's parameter keys")
    if rule_type not in {"amount_limit", "timeliness"}:
        raise ValueError("only numeric amount and deadline rules can be published")
    if len(cases) < 2 or {item.get("expected") for item in cases} != {"PASS", "FAIL"}:
        raise ValueError("replay requires at least one passing and one failing case")
    for value in new_parameters.values():
        if Decimal(str(value)) <= 0:
            raise ValueError("rule limits must be positive")
    if (
        rule_type == "amount_limit"
        and not {"limit", "per_person"} & set(new_parameters)
        and {str(item.get("tier")) for item in cases} != set(new_parameters)
    ):
        raise ValueError("replay must cover every tier in the rule")
    for item in cases:
        if rule_type == "timeliness":
            actual = Decimal(str(item["days"]))
            limit = Decimal(str(new_parameters["days"]))
        else:
            key = (
                "limit"
                if "limit" in new_parameters
                else "per_person"
                if "per_person" in new_parameters
                else str(item["tier"])
            )
            actual = Decimal(str(item["amount"]))
            if key == "per_person":
                actual /= Decimal(str(item["attendees"]))
            limit = Decimal(str(new_parameters[key]))
        actual_result = "PASS" if actual <= limit else "FAIL"
        if actual_result != item["expected"]:
            raise ValueError("candidate fails its rule replay cases")
