"""Offset pagination and page-completeness checks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from config import REPORT_ORDER_BY, REPORT_PAGE_LIMIT, report_rows_path

from .exceptions import PartnerAPIError, ReportNotFoundError, ReportPaginationError
from .models import Page, ReportRows

if TYPE_CHECKING:
    from .client import PartnerAPIClient

logger = logging.getLogger(__name__)


def download_rows(client: PartnerAPIClient, report_id: str) -> ReportRows:
    """Download every page of a completed report.

    Pages with ``offset``, ``limit=1000``, and a stable ``order_by``.
    Offset advances by ``len(page)``, not by the requested limit.

    Args:
        client: Authenticated HTTP client.
        report_id: Id of a job that has reached ``DONE``.

    Returns:
        All raw rows and the partner completeness metadata.

    Raises:
        ReportNotFoundError: If the job is unknown.
        ReportPaginationError: If the report is truncated or pages
            disagree about counts, metadata, or ``has_more``.
        PartnerAPIError: If a page request or payload is invalid.
    """
    offset = 0
    collected: list[dict[str, Any]] = []
    pages = 0
    snapshot: Page | None = None
    while True:
        page = _fetch_page(client, report_id, offset)
        pages += 1
        snapshot = _merge_page(snapshot, page, report_id)
        if page.truncated:
            raise ReportPaginationError(
                f"report {report_id!r} is truncated",
                error="truncated",
            )
        if page.has_more and not page.rows:
            raise ReportPaginationError(
                f"report {report_id!r} has_more=true with an empty page "
                f"at offset={offset}",
                error="empty_page",
            )
        collected.extend(page.rows)
        offset += len(page.rows)
        logger.debug(
            "Fetched page report_id=%s page=%s offset_next=%s "
            "page_rows=%s total=%s has_more=%s",
            report_id,
            pages,
            offset,
            len(page.rows),
            page.total,
            page.has_more,
        )
        if not page.has_more:
            break
        if offset >= page.total:
            raise ReportPaginationError(
                f"report {report_id!r} has_more=true after {offset} of "
                f"{page.total} rows",
                error="inconsistent_total",
            )
    if snapshot is None:
        raise ReportPaginationError(
            f"report {report_id!r} returned no pages",
            error="empty_page",
        )
    if len(collected) != snapshot.total:
        raise ReportPaginationError(
            f"report {report_id!r} fetched {len(collected)} rows "
            f"but total={snapshot.total}",
            error="inconsistent_total",
        )
    logger.info(
        "Fetched rows report_id=%s pages=%s rows=%s total=%s "
        "truncated=%s data_version=%s",
        report_id,
        pages,
        len(collected),
        snapshot.total,
        snapshot.truncated,
        snapshot.data_version,
    )
    return ReportRows(
        rows=collected,
        total=snapshot.total,
        currency=snapshot.currency,
        timezone=snapshot.timezone,
        data_version=snapshot.data_version,
        restated_through=snapshot.restated_through,
        truncated=snapshot.truncated,
    )


def _fetch_page(client: PartnerAPIClient, report_id: str, offset: int) -> Page:
    """Download one report page at ``offset``.

    Requests ``REPORT_PAGE_LIMIT`` rows ordered by ``REPORT_ORDER_BY``.

    Args:
        client: Authenticated HTTP client.
        report_id: Completed report id.
        offset: Number of rows already collected.

    Returns:
        The parsed page.

    Raises:
        ReportNotFoundError: If the partner returns HTTP 404.
        PartnerAPIError: If the request or payload is invalid.
        ReportPaginationError: If the page exceeds the requested limit.
    """
    path = report_rows_path(report_id)
    logger.debug(
        "GET %s offset=%s limit=%s order_by=%s",
        path,
        offset,
        REPORT_PAGE_LIMIT,
        REPORT_ORDER_BY,
    )
    response = client._authenticated(
        "GET",
        path,
        params={
            "offset": offset,
            "limit": REPORT_PAGE_LIMIT,
            "order_by": REPORT_ORDER_BY,
        },
    )
    if response.status_code == 404:
        raise ReportNotFoundError(report_id)
    client._ensure_success(response, path)
    return _parse_page(client, response, path)


def _parse_page(
    client: PartnerAPIClient,
    response: httpx.Response,
    path: str,
) -> Page:
    """Validate one rows payload and return a :class:`Page`.

    Args:
        client: Client used to decode the JSON object.
        response: Successful rows response.
        path: Request path included in error messages.

    Returns:
        Rows plus completeness metadata from this page.

    Raises:
        PartnerAPIError: If rows, totals, or ``meta`` have the wrong type.
        ReportPaginationError: If the page contains more than
            ``REPORT_PAGE_LIMIT`` rows.
    """
    body = client._json_object(response, path)
    rows_raw = body.get("rows")
    if not isinstance(rows_raw, list):
        raise PartnerAPIError(
            f"GET {path} returned invalid rows",
            status_code=response.status_code,
        )
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows_raw):
        if not isinstance(row, dict):
            raise PartnerAPIError(
                f"GET {path} row {index} is not an object",
                status_code=response.status_code,
            )
        rows.append(row)
    if len(rows) > REPORT_PAGE_LIMIT:
        raise ReportPaginationError(
            f"GET {path} returned {len(rows)} rows; limit is {REPORT_PAGE_LIMIT}",
            error="page_too_large",
        )
    total = body.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise PartnerAPIError(
            f"GET {path} returned invalid total",
            status_code=response.status_code,
        )
    has_more = body.get("has_more")
    if not isinstance(has_more, bool):
        raise PartnerAPIError(
            f"GET {path} returned invalid has_more",
            status_code=response.status_code,
        )
    meta = body.get("meta")
    if not isinstance(meta, dict):
        raise PartnerAPIError(
            f"GET {path} returned invalid meta",
            status_code=response.status_code,
        )
    currency = meta.get("currency")
    timezone = meta.get("timezone")
    data_version = meta.get("data_version")
    restated_through = meta.get("restated_through")
    truncated = meta.get("truncated")
    if not isinstance(currency, str) or not currency:
        raise PartnerAPIError(f"GET {path} returned invalid meta.currency")
    if not isinstance(timezone, str) or not timezone:
        raise PartnerAPIError(f"GET {path} returned invalid meta.timezone")
    if not isinstance(data_version, int) or isinstance(data_version, bool):
        raise PartnerAPIError(f"GET {path} returned invalid meta.data_version")
    if not isinstance(restated_through, str) or not restated_through:
        raise PartnerAPIError(
            f"GET {path} returned invalid meta.restated_through",
        )
    if not isinstance(truncated, bool):
        raise PartnerAPIError(f"GET {path} returned invalid meta.truncated")
    return Page(
        rows=rows,
        total=total,
        has_more=has_more,
        currency=currency,
        timezone=timezone,
        data_version=data_version,
        restated_through=restated_through,
        truncated=truncated,
    )


def _merge_page(snapshot: Page | None, page: Page, report_id: str) -> Page:
    """Keep the first page's metadata and reject later pages that disagree.

    Args:
        snapshot: Metadata from earlier pages, or ``None`` on the first page.
        page: Page just fetched.
        report_id: Job id included in error messages.

    Returns:
        ``page`` on the first call, otherwise ``snapshot``.

    Raises:
        ReportPaginationError: If ``total`` or meta fields change. Error
            codes are ``inconsistent_total`` and ``inconsistent_meta``.
    """
    if snapshot is None:
        return page
    if page.total != snapshot.total:
        raise ReportPaginationError(
            f"report {report_id!r} total changed from {snapshot.total} "
            f"to {page.total}",
            error="inconsistent_total",
        )
    if (
        page.currency != snapshot.currency
        or page.timezone != snapshot.timezone
        or page.data_version != snapshot.data_version
        or page.restated_through != snapshot.restated_through
        or page.truncated != snapshot.truncated
    ):
        raise ReportPaginationError(
            f"report {report_id!r} page metadata changed",
            error="inconsistent_meta",
        )
    return snapshot
