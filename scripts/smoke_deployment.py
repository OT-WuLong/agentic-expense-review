"""Run the three synthetic demo cases through the deployed HTTP API."""

from __future__ import annotations

import json
import os
import time
from argparse import ArgumentParser
from uuid import uuid4

import requests

BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CASES = (
    ("GC-A-TRANSPORT-COMMUTE-REJECT", "BUSINESS_REJECTED"),
    ("GC-B-LODGING-MULTIHOP-PASS", "COMPLETED"),
    ("GC-C-DINING-VERSION-CONFLICT-HUMAN", "HUMAN_PENDING"),
)


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[case_id for case_id, _ in CASES])
    selected = parser.parse_args().case
    users = json.loads(os.environ["APP_AUTH_USERS_JSON"])
    token = next(user["token"] for user in users if user["role"] == "FINANCE_REVIEWER")
    client = requests.Session()
    client.headers.update({"Authorization": f"Bearer {token}"})

    def wait_for(request_id: str, expected: str) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get(f"{BASE_URL}/api/v1/approvals/{request_id}", timeout=10)
            response.raise_for_status()
            status = response.json()["approval"]["status"]
            if status == expected:
                print(f"{request_id}: {status}", flush=True)
                return
            if status in {"SYSTEM_ERROR", "BUSINESS_REJECTED", "COMPLETED"}:
                raise RuntimeError(f"{request_id}: unexpected terminal status {status}")
            time.sleep(2)
        raise TimeoutError(f"{request_id}: did not reach {expected}")

    for case_id, expected in CASES:
        if selected and case_id != selected:
            continue
        request_id = f"P15-{case_id.split('-')[1]}-{uuid4().hex[:8]}"
        response = client.post(
            f"{BASE_URL}/api/v1/approvals",
            headers={"Idempotency-Key": f"smoke-create:{request_id}"},
            json={"fixture_case_id": case_id, "request_id": request_id},
            timeout=20,
        )
        response.raise_for_status()
        wait_for(request_id, expected)
        if expected == "HUMAN_PENDING":
            response = client.post(
                f"{BASE_URL}/api/v1/approvals/{request_id}/review",
                json={
                    "action": "APPROVE",
                    "reason": "P15 合成演示复核",
                    "idempotency_key": f"smoke-review:{request_id}",
                },
                timeout=20,
            )
            response.raise_for_status()
            wait_for(request_id, "COMPLETED")


if __name__ == "__main__":
    main()
