"""Retry finalized case-write outbox entries after a worker or model failure."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.approval_service import checkpoint_state
from app.database import PostgresStore
from app.graph.state import validate_state
from app.memory import write_memory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id")
    args = parser.parse_args()
    store = PostgresStore()
    try:
        request_ids = [args.request_id] if args.request_id else store.pending_case_requests()
        for request_id in request_ids:
            checkpoint = checkpoint_state(request_id)
            if checkpoint is None or checkpoint["next_nodes"]:
                print(f"skipped {request_id}: no finalized checkpoint")
                continue
            write_memory(validate_state(checkpoint["state"]), store)
            print(f"processed {request_id}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
