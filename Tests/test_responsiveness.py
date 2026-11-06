# -*- coding: utf-8 -*-
"""버튼 반응 속도 회귀 테스트 — 엑셀 저장·조회가 이벤트 루프(=모든 버튼 응답)를 멈추지 않는지.

예전에는 참석자/득템 저장이 통합문서 전체 로드 + 통계 시트 재계산 + 저장 + 백업을 이벤트 루프에서
그대로 돌려, 그 몇 초 동안 모든 버튼이 응답하지 못했다(3~5초 지연).
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import RaidBook_boss_Bot as b  # noqa: E402
from RaidBook_common import start_loop_stall_monitor  # noqa: E402

TMP = tempfile.mkdtemp(prefix="rb_resp_")
DATA = os.path.join(TMP, "Data")
os.makedirs(os.path.join(DATA, "Backup", "Boss"), exist_ok=True)
b.EXCEL_PATH = os.path.join(DATA, "RaidBook_Boss_Data.xlsx")
b.BOSS_BACKUP_DIR = os.path.join(DATA, "Backup", "Boss")


async def _max_loop_gap(coro):
    """coro가 도는 동안 이벤트 루프가 가장 길게 멈춘 시간(초)과 coro 소요 시간."""
    gaps = []
    done = asyncio.Event()

    async def beat():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.ensure_future(beat())
    t0 = time.monotonic()
    await coro
    took = time.monotonic() - t0
    done.set()
    await t
    return max(gaps or [0]), took


async def _seed(n_msgs=20, per=30):
    for i in range(n_msgs):
        uids = [str(900000 + i * per + k) for k in range(per)]
        await b.save_boss_attendees_to_excel(1000 + i, f"보스{i % 7}", f"{i % 24:02d}:00", uids, "2026-11-01")


def test_save_does_not_block_loop():
    async def run():
        await _seed()
        gap, took = await _max_loop_gap(
            b.save_boss_attendees_to_excel(5000, "보스X", "12:00", [str(u) for u in range(30)], "2026-11-02",
                                           prune=True))
        print(f"  저장 {took:.2f}s 동안 루프 최장 멈춤 {gap * 1000:.0f}ms")
        assert gap < max(0.25, took / 3), (gap, took)
        gap, took = await _max_loop_gap(
            b.save_boss_loot_to_excel(5000, "보스X", "12:00", "반지", "tester"))
        print(f"  득템 저장 {took:.2f}s 동안 루프 최장 멈춤 {gap * 1000:.0f}ms")
        assert gap < max(0.25, took / 3), (gap, took)
        # 저장 직후 미러가 미리 갱신돼 있어야 다음 클릭이 통합문서를 다시 읽지 않는다
        assert b._attend_mirror_fresh()
    asyncio.run(run())
    print("✓ test_save_does_not_block_loop")


def test_attend_view_no_workbook_reload():
    async def run():
        assert b._attend_mirror_fresh()
        calls = []
        real = b.load_workbook
        b.load_workbook = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            v = b.AttendView(message_id=5000)
        finally:
            b.load_workbook = real
        assert not calls, f"통합문서를 {len(calls)}번 다시 열었음"
        assert v.boss_name == "보스X" and len(v.attendees) == 30 and v.loot == "반지", (v.boss_name, v.attendees, v.loot)
    asyncio.run(run())
    print("✓ test_attend_view_no_workbook_reload")


def test_stall_monitor_reports_location():
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    lg = logging.getLogger("rb_stall_test")
    lg.addHandler(H())
    lg.setLevel(logging.INFO)

    def blocking_job():
        time.sleep(0.6)

    async def run():
        t = start_loop_stall_monitor(lg, threshold=0.3, interval=0.05)
        await asyncio.sleep(0.1)
        blocking_job()
        await asyncio.sleep(0.2)
        t.cancel()
    asyncio.run(run())
    hits = [r for r in records if "[응답 지연]" in r]
    assert len(hits) == 1 and "blocking_job" in hits[0], records
    print("✓ test_stall_monitor_reports_location")


if __name__ == "__main__":
    try:
        test_save_does_not_block_loop()
        test_attend_view_no_workbook_reload()
        test_stall_monitor_reports_location()
        print("all passed")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
