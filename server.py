"""Your MCP server for San Francisco 311 data.

FastMCP Gateway runs this file. Its entrypoint setting is `server.py:mcp`.
"""

import logging
import re
from collections import Counter, defaultdict
from statistics import median

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from sf311.data import BY_ID, LOCAL_NAMES, NEIGHBORHOODS, REQUESTS, SERVICES, closest
from sf311.kit import caller_email

mcp = FastMCP("sf-311")
logger = logging.getLogger("sf311")

HOUSE_NUMBER = re.compile(r"^(\d+)\s+([^,]+)")


def _public_row(request) -> dict:
    """Request as a dict with the address reduced to the block."""
    row = request.as_dict()
    match = HOUSE_NUMBER.match(row["address"])
    if match:
        row["address"] = f"{int(match.group(1)) // 100 * 100} block of {match.group(2).strip()}"
    return row


@mcp.tool(annotations={"readOnlyHint": True})
def dataset_info() -> dict:
    """Describe the 311 dataset this server answers questions about.

    Call this first to learn the date range and the exact service and
    neighborhood names that other tools accept.
    """
    return {
        "description": "San Francisco 311 service requests filed in one week.",
        "first_opened": min(request.opened for request in REQUESTS).isoformat(),
        "last_opened": max(request.opened for request in REQUESTS).isoformat(),
        "requests": len(REQUESTS),
        "services": SERVICES,
        "neighborhoods": NEIGHBORHOODS,
        "local_neighborhood_names": len(LOCAL_NAMES),
    }


MAX_ROWS = 50
MIN_CLOSED_FOR_MEDIAN = 10
STATUSES = ["Open", "Closed"]
GROUPS = {
    "neighborhood": lambda r: r.neighborhood,
    "service": lambda r: r.service,
    "agency": lambda r: r.agency,
    "source": lambda r: r.source,
    "weekday": lambda r: r.opened.strftime("%A"),
    "status": lambda r: r.status,
}
MEDIAN_GROUPS = ("neighborhood", "agency")


def _check_name(kind: str, value: str | None, choices: list[str]) -> None:
    if value is None or value in choices:
        return
    suggestions = closest(value, choices)
    hint = f"Did you mean: {', '.join(suggestions)}?" if suggestions else "Call dataset_info to see valid names."
    raise ToolError(f"Unknown {kind} {value!r}. {hint}")


def _check_group(group_by: str | None, allowed) -> None:
    if group_by is not None and group_by not in allowed:
        raise ToolError(f"Unknown group_by {group_by!r}. Use one of: {', '.join(allowed)}.")


def _matching(service: str | None, neighborhood: str | None, status: str | None = None) -> list:
    _check_name("service", service, SERVICES)
    _check_name("neighborhood", neighborhood, NEIGHBORHOODS)
    if status is not None and status not in STATUSES:
        raise ToolError(f"Unknown status {status!r}. Use 'Open' or 'Closed'.")
    return [
        r
        for r in REQUESTS
        if (service is None or r.service == service)
        and (neighborhood is None or r.neighborhood == neighborhood)
        and (status is None or r.status == status)
    ]


@mcp.tool(annotations={"readOnlyHint": True})
def search_requests(
    service: str | None = None,
    neighborhood: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List individual 311 requests, newest first, so you can see examples.

    Returns up to `limit` requests (1 to 50, default 20) with request_id, opened,
    closed, status, service, subtype, agency, neighborhood, local_name, address
    (masked to the block, e.g. "900 block of HARRISON ST") and source. Filter by exact `service` and `neighborhood` names from
    dataset_info, and by `status` ("Open" or "Closed"); all filters are optional.

    Do not use this to count or rank: it returns at most 50 rows, so use
    count_requests instead. For one known request, use get_request. For response
    times, use median_close_hours.
    """
    if not 1 <= limit <= MAX_ROWS:
        raise ToolError(f"limit must be between 1 and {MAX_ROWS}. To count more than {MAX_ROWS} requests, use count_requests.")
    rows = sorted(_matching(service, neighborhood, status), key=lambda r: r.opened, reverse=True)
    return [_public_row(r) for r in rows[:limit]]


@mcp.tool(annotations={"readOnlyHint": True})
def count_requests(
    service: str | None = None,
    neighborhood: str | None = None,
    group_by: str | None = None,
) -> int | dict[str, int]:
    """Count 311 requests this week, optionally broken down by a field.

    With no `group_by`, returns a single number. With `group_by` (one of
    neighborhood, service, agency, source, weekday, status), returns a dict of
    group to count, largest first, at most 50 groups; requests with a blank
    value for that field are left out. Filter by exact `service` and
    `neighborhood` names from dataset_info.

    Use this for "how many" and "which has the most" questions, for example
    group_by="neighborhood" with a service to find the busiest neighborhood.
    To read individual requests use search_requests; for how long requests take
    to close use median_close_hours.
    """
    _check_group(group_by, GROUPS)
    rows = _matching(service, neighborhood)
    if group_by is None:
        return len(rows)
    key = GROUPS[group_by]
    counts = Counter(key(r) for r in rows if key(r))
    pairs = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(pairs[:MAX_ROWS])


@mcp.tool(annotations={"readOnlyHint": True})
def count_by_hour(
    service: str | None = None,
    neighborhood: str | None = None,
) -> dict[int, int]:
    """Count 311 requests by the hour of day they were opened (0 to 23, San Francisco local time).

    Returns a dict of hour to count, busiest hour first, ties broken by the
    earlier hour; hours with no requests are left out. Optionally filter by
    exact `service` and `neighborhood` names from dataset_info.

    Use this for "what time of day" or "which hour" questions. For counts by
    weekday, neighborhood or service use count_requests; to read individual
    requests use search_requests.
    """
    counts = Counter(r.opened.hour for r in _matching(service, neighborhood))
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


@mcp.tool(annotations={"readOnlyHint": True})
def median_close_hours(
    service: str | None = None,
    group_by: str | None = None,
) -> float | dict[str, float] | None:
    """Median hours from opened to closed for requests that have closed.

    With no `group_by`, returns one number rounded to 0.1 (null if nothing has
    closed). With `group_by` ("neighborhood" or "agency"), returns a dict of
    group to median hours, fastest first, leaving out groups with fewer than 10
    closed requests. Optionally filter by an exact `service` name from
    dataset_info. Open requests are ignored.

    Use this for "how fast" or "how long" questions. For counts use
    count_requests; for individual requests use search_requests.
    """
    _check_group(group_by, MEDIAN_GROUPS)
    rows = [r for r in _matching(service, None) if r.hours_to_close is not None]
    if group_by is None:
        return round(median(r.hours_to_close for r in rows), 1) if rows else None
    key = GROUPS[group_by]
    hours = defaultdict(list)
    for r in rows:
        if key(r):
            hours[key(r)].append(r.hours_to_close)
    medians = {g: round(median(v), 1) for g, v in hours.items() if len(v) >= MIN_CLOSED_FOR_MEDIAN}
    return dict(sorted(medians.items(), key=lambda item: (item[1], item[0]))[:MAX_ROWS])


@mcp.tool(annotations={"readOnlyHint": True})
def get_request(request_id: str) -> dict:
    """Fetch one 311 request by its request_id, with all its fields.

    The address is masked to the block. Each call is logged with the caller.

    Use this when you already have an id (for example from search_requests).
    To find requests by service or neighborhood use search_requests; to count
    them use count_requests.
    """
    request = BY_ID.get(request_id)
    if request is None:
        raise ToolError(f"No request with id {request_id!r}. Use search_requests to find valid request_ids.")
    logger.info("get_request %s by %s", request_id, caller_email() or "local")
    return _public_row(request)


if __name__ == "__main__":
    mcp.run()
