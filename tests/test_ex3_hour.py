"""Exercise 3 challenge: a tool that answers "which hour of the day?" in one call."""

from collections import Counter

import pytest
from fastmcp.exceptions import ToolError

import reference


def _expected(service=None, neighborhood=None) -> dict[int, int]:
    counts = Counter(
        r.opened.hour
        for r in reference.REQUESTS
        if (service is None or r.service == service) and (neighborhood is None or r.neighborhood == neighborhood)
    )
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


async def test_count_by_hour_is_read_only_and_documented(client):
    tool = next(t for t in await client.list_tools() if t.name == "count_by_hour")
    assert tool.description
    assert tool.annotations and tool.annotations.read_only_hint


@pytest.mark.parametrize("arguments", [{}, {"service": "Tree Maintenance"}, {"service": "Encampment", "neighborhood": "Mission"}])
async def test_count_by_hour_matches_the_data(client, arguments):
    result = await client.call_tool("count_by_hour", arguments)
    assert {int(h): c for h, c in result.data.items()} == _expected(**arguments)


async def test_busiest_tree_maintenance_hour_comes_first(client):
    result = await client.call_tool("count_by_hour", {"service": "Tree Maintenance"})
    assert int(next(iter(result.data))) == next(iter(_expected("Tree Maintenance")))


async def test_count_by_hour_rejects_unknown_service(client):
    with pytest.raises(ToolError, match="Did you mean"):
        await client.call_tool("count_by_hour", {"service": "Tree Maintenence"})
