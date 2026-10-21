"""Bounded retries, Retry-After handling, and a local /v2 rate limiter."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from config import REPORTS_PATH, Settings

from .exceptions import AmbiguousRequestError, RetryExhaustedError

logger = logging.getLogger(__name__)

_TOKEN_RETRY_ERRORS = frozenset({"token_expired", "invalid_token"})


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per rolling window.

    Args:
        limit: Maximum events inside ``window_s``. Zero or less disables
            waiting.
        window_s: Window length in seconds. Defaults to 60.
    """

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        """Store the limit and start with an empty event history."""
        self._limit = limit
        self._window_s = window_s
        self._times: list[float] = []

    def wait(self) -> None:
        """Block until a slot is free, then record this event.

        A limit of zero or less returns immediately and records nothing.
        The wait is based on ``time.monotonic`` and may call ``time.sleep``.
        """
        if self._limit <= 0:
            return
        now = time.monotonic()
        self._times = [stamp for stamp in self._times if now - stamp < self._window_s]
        if len(self._times) >= self._limit:
            delay = self._window_s - (now - self._times[0])
            if delay > 0:
                logger.debug("Local /v2 limiter sleeping %.3fs", delay)
                time.sleep(delay)
            now = time.monotonic()
            self._times = [
                stamp for stamp in self._times if now - stamp < self._window_s
            ]
        self._times.append(time.monotonic())


def partner_error_code(response: httpx.Response) -> str | None:
    """Read the partner error code from a flat or nested JSON body.

    Accepts ``{"error": ...}``, FastAPI's ``{"detail": {"error": ...}}``,
    and a string ``detail``. Non-JSON and non-object bodies have no code.

    Args:
        response: HTTP response whose body may contain an error code.

    Returns:
        The code as a string, or ``None`` when the body does not carry one.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        error = detail.get("error")
        return str(error) if error is not None else None
    if isinstance(detail, str) and detail:
        return detail
    error = payload.get("error")
    return str(error) if error is not None else None


def is_token_refresh_error(error: str | None) -> bool:
    """Return whether this 401 code should trigger one token refresh.

    Args:
        error: Partner error code, or ``None`` when the body had none.

    Returns:
        ``True`` for ``token_expired`` and ``invalid_token`` only.
    """
    return error in _TOKEN_RETRY_ERRORS


def retry_after_seconds(
    response: httpx.Response, default: float, buffer_s: float
) -> float:
    """Parse ``Retry-After`` as whole seconds and add ``buffer_s``.

    An HTTP-date value, or any non-integer header, falls back to ``default``.
    Negative waits and buffers are treated as zero before they are added.

    Args:
        response: Response that may include ``Retry-After``.
        default: Seconds used when the header is missing or not an integer.
        buffer_s: Extra seconds added for a rolling rate-limit window.

    Returns:
        Non-negative seconds to sleep.
    """
    raw = response.headers.get("Retry-After")
    wait = default
    if raw is not None:
        try:
            wait = float(int(str(raw).strip()))
        except ValueError:
            wait = default
    return max(0.0, wait) + max(0.0, buffer_s)


def backoff_seconds(attempt: int, settings: Settings) -> float:
    """Return exponential backoff for the attempt that just failed.

    Delay is ``retry_backoff_s * 2 ** (attempt - 1)``, capped at
    ``retry_backoff_cap_s``. Attempt numbers are 1-based.

    Args:
        attempt: The try that just failed. ``1`` uses the initial backoff.
        settings: Backoff base and cap.

    Returns:
        Seconds to sleep before the next try.
    """
    delay = settings.retry_backoff_s * (2 ** (attempt - 1))
    return min(delay, settings.retry_backoff_cap_s)


def is_ambiguous_post(method: str, path: str) -> bool:
    """Return whether this call is ``POST /v2/reports``.

    That request can create a job before the client sees a response, so a
    transport or 5xx failure must not be replayed.

    Args:
        method: HTTP method. Compared case-insensitively.
        path: Request path. One trailing slash is ignored.

    Returns:
        ``True`` only for ``POST`` to ``REPORTS_PATH``.
    """
    return method.upper() == "POST" and path.rstrip("/") == REPORTS_PATH


def execute_with_retries(
    send: Callable[[], httpx.Response],
    method: str,
    path: str,
    settings: Settings,
    limiter: SlidingWindowLimiter | None,
) -> httpx.Response:
    """Send ``method path``, retrying temporary failures.

    Retries transport errors, HTTP 5xx, and HTTP 429. Does not retry HTTP 400.
    Does not retry transport/5xx on POST /v2/reports (duplicate-job risk).

    Args:
        send: Zero-argument callable that performs one HTTP attempt.
        method: HTTP method, for logging and POST classification.
        path: Request path, for logging, limiting, and POST classification.
        settings: Retry and backoff configuration.
        limiter: Optional local limiter applied to ``/v2`` paths.

    Returns:
        A successful response, or a non-retryable error response (typically 4xx).

    Raises:
        AmbiguousRequestError: POST /v2/reports failed after the request may
            have reached the partner.
        RetryExhaustedError: Retryable failures continued until ``max_attempts``.
        PartnerAPIError: Unexpected transport wrapper used by callers.
    """
    ambiguous = is_ambiguous_post(method, path)
    last_status: int | None = None
    last_error: str | None = None
    attempts = settings.max_attempts
    for attempt in range(1, attempts + 1):
        if limiter is not None and path.startswith("/v2/"):
            limiter.wait()
        try:
            response = send()
        except httpx.RequestError as exc:
            if ambiguous:
                raise AmbiguousRequestError(
                    f"{method} {path} failed after leaving the client; "
                    "not retried to avoid duplicate jobs",
                    error="ambiguous_post",
                ) from exc
            last_error = "transport_error"
            if attempt >= attempts:
                raise RetryExhaustedError(
                    f"{method} {path} failed after {attempt} attempts: {exc}",
                    error="transport_error",
                    attempts=attempt,
                ) from exc
            delay = backoff_seconds(attempt, settings)
            logger.warning(
                "Retry %s %s attempt=%s/%s transport_error sleep=%.3fs",
                method,
                path,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
            continue

        if response.is_success:
            return response

        last_status = response.status_code
        last_error = partner_error_code(response)
        if response.status_code == 429:
            delay = retry_after_seconds(
                response,
                default=backoff_seconds(attempt, settings),
                buffer_s=settings.retry_after_buffer_s,
            )
            if attempt >= attempts:
                break
            logger.warning(
                "Retry %s %s attempt=%s/%s HTTP 429 error=%s sleep=%.3fs",
                method,
                path,
                attempt,
                attempts,
                last_error,
                delay,
            )
            time.sleep(delay)
            continue
        if 500 <= response.status_code <= 599:
            if ambiguous:
                raise AmbiguousRequestError(
                    f"{method} {path} failed with HTTP {response.status_code}; "
                    "not retried to avoid duplicate jobs",
                    status_code=response.status_code,
                    error=last_error or "ambiguous_post",
                )
            if attempt >= attempts:
                break
            delay = backoff_seconds(attempt, settings)
            logger.warning(
                "Retry %s %s attempt=%s/%s HTTP %s error=%s sleep=%.3fs",
                method,
                path,
                attempt,
                attempts,
                response.status_code,
                last_error,
                delay,
            )
            time.sleep(delay)
            continue
        return response

    raise RetryExhaustedError(
        f"{method} {path} failed after {attempts} attempts: "
        f"HTTP {last_status} {last_error or ''}".rstrip(),
        status_code=last_status,
        error=last_error,
        attempts=attempts,
    )
