"""Shared HTTP mock fixtures for partner API client tests."""

from __future__ import annotations

import json
from typing import Any

import httpx

from api_client import PartnerAPIClient
from config import ACCOUNT_PATH, OAUTH_TOKEN_PATH, REPORTS_PATH, Settings

VALID_SETTINGS = Settings(
    api_base="http://partner.test",
    client_id="acme-de-takehome",
    client_secret="s3cr3t-do-not-commit",
)

ACCOUNT_BODY = {
    "account_id": "acct_5512",
    "timezone": "America/Los_Angeles",
    "currency": "EUR",
    "attribution_window_days": 3,
}

ROW_META = {
    "currency": "EUR",
    "timezone": "America/Los_Angeles",
    "data_version": 42,
    "restated_through": "2026-10-19",
    "truncated": False,
}


class FakeClock:
    """Monotonic clock that advances only when sleep() is called."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class MockAPI:
    """Minimal mock of the partner token, account, and report endpoints."""

    def __init__(self, expires_in: int = 300) -> None:
        self.expires_in = expires_in
        self.token_calls = 0
        self.account_calls = 0
        self.submit_calls = 0
        self.status_calls = 0
        self.last_token_body: dict | None = None
        self.last_authorization: str | None = None
        self.last_submit_body: dict[str, Any] | None = None
        self.report_id = "rep_1"
        self.status_queue: list[dict[str, Any]] = []
        self.status_default: dict[str, Any] = {
            "report_id": "rep_1",
            "status": "DONE",
            "row_count": 12,
        }
        self.missing_ids: set[str] = set()
        self.row_pages: list[dict[str, Any]] = []
        self.row_calls = 0
        self.row_params: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == OAUTH_TOKEN_PATH:
            return self._token(request)
        if request.method == "GET" and request.url.path == ACCOUNT_PATH:
            return self._account(request)
        if request.method == "POST" and request.url.path == REPORTS_PATH:
            return self._submit(request)
        if request.method == "GET" and request.url.path.endswith("/rows"):
            return self._rows(request)
        if request.method == "GET" and request.url.path.startswith(f"{REPORTS_PATH}/"):
            return self._status(request)
        return httpx.Response(404, json={"detail": {"error": "not_found"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        body = json.loads(request.content)
        self.last_token_body = body
        if (
            body.get("client_id") != VALID_SETTINGS.client_id
            or body.get("client_secret") != VALID_SETTINGS.client_secret
        ):
            return httpx.Response(
                401,
                json={"detail": {"error": "invalid_client"}},
            )
        return httpx.Response(
            200,
            json={
                "access_token": f"tok_{self.token_calls}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
            },
        )

    def _account(self, request: httpx.Request) -> httpx.Response:
        denied = self._unauthorized(request)
        if denied is not None:
            return denied
        self.account_calls += 1
        return httpx.Response(200, json=ACCOUNT_BODY)

    def _unauthorized(self, request: httpx.Request) -> httpx.Response | None:
        self.last_authorization = request.headers.get("Authorization")
        if self.last_authorization != f"Bearer tok_{self.token_calls}":
            return httpx.Response(
                401,
                json={"detail": {"error": "invalid_token"}},
            )
        return None

    def _submit(self, request: httpx.Request) -> httpx.Response:
        denied = self._unauthorized(request)
        if denied is not None:
            return denied
        self.submit_calls += 1
        self.last_submit_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"report_id": self.report_id, "status": "PENDING"},
        )

    def _status(self, request: httpx.Request) -> httpx.Response:
        denied = self._unauthorized(request)
        if denied is not None:
            return denied
        report_id = request.url.path.rsplit("/", 1)[-1]
        self.status_calls += 1
        if report_id in self.missing_ids:
            return httpx.Response(
                404,
                json={"detail": {"error": "unknown_report"}},
            )
        if self.status_queue:
            body = self.status_queue.pop(0)
        else:
            body = self.status_default
        return httpx.Response(200, json=body)

    def _rows(self, request: httpx.Request) -> httpx.Response:
        denied = self._unauthorized(request)
        if denied is not None:
            return denied
        report_id = request.url.path.removesuffix("/rows").rsplit("/", 1)[-1]
        self.row_calls += 1
        self.row_params.append(dict(request.url.params))
        if report_id in self.missing_ids:
            return httpx.Response(
                404,
                json={"detail": {"error": "unknown_report"}},
            )
        if not self.row_pages:
            return httpx.Response(
                404,
                json={"detail": {"error": "unknown_report"}},
            )
        return httpx.Response(200, json=self.row_pages.pop(0))


def row(index: int) -> dict[str, Any]:
    """Build a minimal report row for pagination tests."""
    return {
        "date": "2026-10-01",
        "country": "US",
        "channel": "search",
        "impressions": index,
    }


def page(
    rows: list[dict[str, Any]],
    *,
    total: int,
    has_more: bool,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``/rows`` JSON body."""
    return {
        "rows": rows,
        "total": total,
        "has_more": has_more,
        "meta": dict(ROW_META if meta is None else meta),
    }


def make_client(
    handler: MockAPI,
    settings: Settings = VALID_SETTINGS,
) -> PartnerAPIClient:
    """Return a client wired to ``MockAPI`` via httpx.MockTransport."""
    return PartnerAPIClient(settings, transport=httpx.MockTransport(handler))
