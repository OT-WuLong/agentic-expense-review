"""Small single-company token login and signed same-origin browser session."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.api_models import DemoIdentity

COOKIE_NAME = "approval_session"
SESSION_SECONDS = 8 * 60 * 60


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _config() -> tuple[bytes, list[tuple[str, DemoIdentity]]]:
    secret = os.getenv("APP_SESSION_SECRET", "")
    raw_users = os.getenv("APP_AUTH_USERS_JSON", "")
    if len(secret) < 32 or not raw_users:
        raise HTTPException(status_code=503, detail="身份认证尚未配置")
    try:
        entries = json.loads(raw_users)
        users = [
            (
                entry["token"],
                DemoIdentity.model_validate(
                    {key: value for key, value in entry.items() if key != "token"}
                ),
            )
            for entry in entries
        ]
        if (
            not users
            or any(len(token) < 24 for token, _ in users)
            or len({token for token, _ in users}) != len(users)
        ):
            raise ValueError("short or empty token set")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="身份认证配置无效") from exc
    return secret.encode(), users


def authenticate_token(token: str) -> DemoIdentity:
    _, users = _config()
    for configured, identity in users:
        if _equal(token, configured):
            return identity
    raise HTTPException(status_code=401, detail="访问令牌无效")


def _session_token(token: str) -> str:
    secret, _ = _config()
    claims = {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expires": int(time.time()) + SESSION_SECONDS,
        "csrf": secrets.token_urlsafe(24),
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def start_session(response: Response, token: str) -> str:
    authenticate_token(token)
    value = _session_token(token)
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=SESSION_SECONDS,
        path="/api/v1",
        httponly=True,
        secure=os.getenv("APP_COOKIE_SECURE") == "1",
        samesite="strict",
    )
    claims = _claims(value)
    return str(claims["csrf"])


def end_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/api/v1")


def _claims(value: str) -> dict[str, Any]:
    secret, _ = _config()
    try:
        payload, signature = value.split(".", 1)
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not _equal(signature, expected):
            raise ValueError("invalid signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(data["expires"]) <= time.time():
            raise ValueError("expired session")
        if not isinstance(data["token_hash"], str) or not isinstance(data["csrf"], str):
            raise TypeError("invalid session fields")
        return data
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=401, detail="会话无效或已过期") from exc


def identity_from_request(request: Request) -> DemoIdentity:
    authorization = request.headers.get("Authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="访问令牌格式无效")
        request.state.csrf_token = None
        return authenticate_token(token)
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=401, detail="请先登录")
    claims = _claims(cookie)
    request.state.session_expires_at = int(claims["expires"])
    _, users = _config()
    for token, identity in users:
        if _equal(claims["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
            csrf = str(claims["csrf"])
            request.state.actor_id = identity.actor_id
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not _equal(
                request.headers.get("X-CSRF-Token", ""), csrf
            ):
                raise HTTPException(status_code=403, detail="CSRF 校验失败")
            request.state.csrf_token = csrf
            return identity
    raise HTTPException(status_code=401, detail="会话对应的身份已失效")
