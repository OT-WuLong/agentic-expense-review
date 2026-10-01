"""Print local P13 credentials to paste into .env; never writes secrets to disk."""

import json
import os
import secrets
import sys


def main() -> None:
    if sys.argv[1:] == ["--show"]:
        configured = os.getenv("APP_AUTH_USERS_JSON", "")
        if not configured:
            raise SystemExit("APP_AUTH_USERS_JSON is not configured")
        for user in json.loads(configured):
            print(f"{user['role']} / {user['actor_id']}: {user['token']}")
        return
    if sys.argv[1:]:
        raise SystemExit("usage: generate_auth_config.py [--show]")
    users = [
        ("EMP-DEMO-001", "演示申请人", "APPLICANT", ["DEPT-SYN-RD"]),
        ("EMP-DEMO-002", "销售申请人", "APPLICANT", ["DEPT-SALES"]),
        (
            "FIN-DEMO-001",
            "财务复核员",
            "FINANCE_REVIEWER",
            [
                "DEPT-SYN-RD",
                "DEPT-SYN-EAST-SALES",
                "DEPT-SYN-MARKETING",
                "DEPT-OPERATIONS",
                "DEPT-SALES",
                "DEPT-MARKETING",
            ],
        ),
        ("RULE-DEMO-001", "规则管理员", "RULE_ADMIN", []),
        ("SYS-DEMO-001", "系统管理员", "SYSTEM_ADMIN", []),
    ]
    records = [
        {
            "token": secrets.token_urlsafe(32),
            "actor_id": actor_id,
            "display_name": name,
            "role": role,
            "department_ids": departments,
        }
        for actor_id, name, role, departments in users
    ]
    print(f"APP_SESSION_SECRET={secrets.token_urlsafe(48)}")
    print(f"APP_AUTH_USERS_JSON='{json.dumps(records, ensure_ascii=True, separators=(',', ':'))}'")
    print("APP_COOKIE_SECURE=0")
    print("# Keep this output private; use APP_COOKIE_SECURE=1 behind HTTPS.")


if __name__ == "__main__":
    main()
