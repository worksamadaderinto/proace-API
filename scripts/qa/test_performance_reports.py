"""Regression coverage for misleading benchmark success claims."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx

from catalog_workload import discover_catalog, route_api_request
from stress_test import StressTester
from volume_test import VolumeTester


def test_discovery_uses_server_page_count_and_actual_category() -> None:
    payload = {
        "count": 323,
        "pages": 14,
        "results": [{"id": 1, "category": "Home & garden", "title": "Real title"}],
    }

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            fixtures = await discover_catalog(client, "http://testserver")
            assert fixtures["pages"] == 14
            assert fixtures["categories"] == ["Home & garden"]

    asyncio.run(run())


def test_proxy_routing_preserves_query_encoding() -> None:
    request = httpx.Request(
        "GET",
        "http://testserver/api/v1/products/?search=Home%20%26%20garden",
    )
    with patch.dict("os.environ", {"PERFORMANCE_API_PREFIX": "/api"}):
        asyncio.run(route_api_request(request))
    assert request.url.path == "/api/products"
    assert request.url.query == b"search=Home%20%26%20garden"


def test_filter_sampling_does_not_hide_an_earlier_failure() -> None:
    responses = iter([404, 200, 200])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    next(responses), json={"results": [{"id": 1}]}
                )
            )
        ) as client:
            sample = await VolumeTester("http://testserver").sample(
                client, "/api/v1/products/"
            )
            assert sample["statuses"] == [404, 200, 200]
            assert sample["result_counts"] == [0, 1, 1]

    asyncio.run(run())


def test_stress_counts_client_errors_and_respects_latency() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404, json={}))
        ) as client:
            data = await StressTester("http://testserver").ramp_stage(client, 2, 1)
            assert data["error_rate_pct"] == 100

    asyncio.run(run())
    tester = StressTester("http://testserver")
    report = {
        "target_url": "http://testserver",
        "max_concurrency": 20,
        "ramp_stages": [
            {
                "concurrency": 20,
                "requests": 120,
                "rps": 20,
                "stats": {"p50": 1000, "p95": 6000, "max": 8000},
                "error_rate_pct": 0,
            }
        ],
        "spike_test": {
            "burst_size": 80,
            "duration_s": 4,
            "stats": {"p95": 1500},
            "errors": 80,
            "recovery_status": 503,
            "recovery_latency_ms": 200,
        },
        "race_condition_test": {"status": "SKIPPED", "overselling_detected": None},
    }
    markdown = tester.generate_markdown(report)
    assert "**Overall Verdict**: **FAIL**" in markdown
    assert "| FAIL |" in markdown
    assert "Not verified" in markdown
    assert "Zero negative inventory detected" not in markdown


def test_inventory_without_evidence_is_skipped() -> None:
    with patch.dict("os.environ", {}, clear=True):
        data = asyncio.run(StressTester("http://testserver").race_condition_lock_test())
    assert data["overselling_detected"] is None
    assert data["status"].startswith("SKIPPED")


def test_load_does_not_mix_fast_404s_with_successful_latency() -> None:
    from load_test import LoadTester

    tester = LoadTester("http://testserver", 1, 60)
    tester.results = [
        {
            "scenario": "Catalog Listing",
            "status": 200,
            "latency_ms": 1000,
            "result_count": 24,
        },
        {
            "scenario": "Catalog Search",
            "status": 404,
            "latency_ms": 1,
            "result_count": None,
        },
    ]
    with patch("load_test.save_report", return_value=("ignored.md", "latest.md")):
        data = tester.compile_report(60)
    assert data["verdict"] == "FAIL"
    assert data["successful_latency_ms"]["p95"] == 1000
    assert (
        data["scenario_breakdown"]["Catalog Search"]["successful_latency_ms"]["count"]
        == 0
    )
    assert "{404: 1}" in tester.generate_markdown(data)


def test_server_timing_parser_rejects_unknown_and_nonfinite_data() -> None:
    from catalog_workload import server_timings
    assert server_timings('db;dur=12.5, search_server;dur=4, app;dur=NaN, hydrate;dur=-1, secret;dur=9, serialize;dur=bad') == {"db": 12.5, "search_server": 4.0}


def test_latest_report_preserves_unrelated_artifacts(tmp_path) -> None:
    import utils
    unrelated = tmp_path / "fixture.json"
    unrelated.write_text("keep")
    (tmp_path / "load-report-old.md").write_text("superseded")
    with patch.object(utils, "get_report_dir", return_value=tmp_path):
        utils.save_report("load", "load-report", "completed", {"requests": 3})
    assert (tmp_path / "latest.md").read_text() == "completed"
    import json
    assert json.loads((tmp_path / "latest.json").read_text()) == {"requests": 3}
    assert not (tmp_path / "load-report-old.md").exists()
    assert unrelated.read_text() == "keep"


def test_slow_search_is_not_hidden_by_fast_listing_mix() -> None:
    from load_test import LoadTester
    tester = LoadTester("http://testserver", 1, 60)
    tester.results = [{"scenario": "Catalog Listing", "status": 200, "latency_ms": 10, "result_count": 24} for _ in range(100)]
    tester.results.append({"scenario": "Catalog Search", "status": 200, "latency_ms": 900, "result_count": 1})
    with patch("load_test.save_report", return_value=("latest.md", "latest.md")):
        data = tester.compile_report(60)
    assert data["latency_stats_ms"]["p95"] < 450
    assert data["verdict"] == "FAIL"
    assert "Catalog Search: p95/p99 latency target exceeded" in data["failure_reasons"]


def test_card_workload_routes_only_listings_and_preserves_filters() -> None:
    request = httpx.Request("GET", "http://testserver/api/v1/products/?search=Home%20%26%20garden")
    detail = httpx.Request("GET", "http://testserver/api/v1/products/42/")
    with patch.dict("os.environ", {"PERFORMANCE_CATALOG_VIEW": "card", "PERFORMANCE_API_PREFIX": "/api/v1"}):
        asyncio.run(route_api_request(request))
        asyncio.run(route_api_request(detail))
    assert request.url.params["view"] == "card"
    assert request.url.params["search"] == "Home & garden"
    assert "view" not in detail.url.params
