"""Tools answer within the time budget and finish slow first-time redactions in the background."""

from __future__ import annotations

import asyncio
import time

from mcp import Client

from knee_mcp.config import Config
from knee_mcp.server import build_server
from knee_mcp.service import KneeService


def _slow_service(tmp_path, drive, delay: float, budget: float) -> KneeService:
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=2, tool_budget=budget, exclude_folders=["匿名化"])
    svc = KneeService(cfg, drive)
    real = svc.proc.redacted

    def slow(f):
        if not svc.proc.is_cached(f):  # only first-time redaction is slow, like on the VPS
            time.sleep(delay)
        return real(f)

    svc.proc.redacted = slow  # type: ignore[method-assign]
    return svc


async def test_get_slices_returns_pending_then_images(tmp_path, drive):
    svc = _slow_service(tmp_path, drive, delay=3.0, budget=0.3)
    server = build_server(svc.cfg, svc)
    args = {"study": "2019-03-20", "series": "7", "start": 1, "end": 4}
    async with Client(server) as c:
        t = time.monotonic()
        first = await c.call_tool("get_slices", args)
        assert time.monotonic() - t < 3  # answered within the budget, not after all redactions
        assert not first.is_error
        texts = [x.text for x in first.content if x.type == "text"]
        assert any("not prebuilt yet (queued for masking): I0001, I0002, I0003, I0004" in t for t in texts)

        for _ in range(60):  # background work keeps going after the first answer
            await asyncio.sleep(0.5)
            again = await c.call_tool("get_slices", args)
            if [x.type for x in again.content].count("image") == 4:
                break
        assert [x.type for x in again.content].count("image") == 4
        assert not any("not prebuilt" in x.text for x in again.content if x.type == "text")


async def test_get_slice_and_compare_say_still_masking(tmp_path, drive):
    svc = _slow_service(tmp_path, drive, delay=2.0, budget=0.2)
    server = build_server(svc.cfg, svc)
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 9})
        cmp = await c.call_tool("compare", {"a": {"study": "2019-03-20", "series": "7", "index": 10},
                                            "b": {"study": "2024-12-03", "series": "4", "index": 1}})
    assert r.is_error and "not prebuilt yet" in r.content[0].text
    assert cmp.is_error and "not prebuilt yet" in cmp.content[0].text


async def test_contact_sheet_marks_pending_tiles(tmp_path, drive):
    svc = _slow_service(tmp_path, drive, delay=2.0, budget=0.2)
    server = build_server(svc.cfg, svc)
    async with Client(server) as c:
        r = await c.call_tool("get_contact_sheet", {"study": "2024-12-03", "series": "4"})
    assert not r.is_error and r.content[0].type == "image"
    assert "not prebuilt yet" in r.content[1].text


async def test_cached_images_do_not_wait_behind_slow_jobs(tmp_path, drive):
    svc = _slow_service(tmp_path, drive, delay=0.0, budget=30)
    svc.cfg.workers = 1
    server = build_server(svc.cfg, svc)
    async with Client(server) as c:
        await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 2})  # now cached
        svc_slow = svc.proc.redacted

        def very_slow(f):
            if not svc.proc.is_cached(f):
                time.sleep(5)
            return svc_slow(f)

        svc.proc.redacted = very_slow  # type: ignore[method-assign]
        svc.cfg.tool_budget = 0.5
        await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 3})  # occupies the worker
        t = time.monotonic()
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 2})
        assert not r.is_error and r.content[0].type == "image" and time.monotonic() - t < 2


def test_duplicate_requests_share_one_job(tmp_path, drive):
    svc = _slow_service(tmp_path, drive, delay=1.0, budget=0.1)
    f = svc.catalog.index().get_study("2019-03-20").series[7].get(1).file
    assert svc._submit(f) is svc._submit(f)


def test_stale_index_refreshes_in_background(tmp_path, drive):
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", listing_ttl=0.0)
    svc = KneeService(cfg, drive)
    first = svc.catalog.index()
    calls = []
    real = drive.list_files

    def slow_list():
        calls.append(1)
        time.sleep(1.5)
        return real()

    drive.list_files = slow_list
    time.sleep(0.05)  # let the (coarse, ~15 ms on Windows) monotonic clock pass the TTL
    t = time.monotonic()
    again = svc.catalog.index()  # stale: must not block on the slow listing
    assert again is first and time.monotonic() - t < 0.5
    for _ in range(40):
        time.sleep(0.1)
        if svc.catalog._index is not first:
            break
    assert svc.catalog._index is not first and calls
