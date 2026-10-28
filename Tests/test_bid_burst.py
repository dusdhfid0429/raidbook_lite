# -*- coding: utf-8 -*-
"""경매 입찰 연타 — 짧은 간격으로 몰린 클릭이 모두 판정되고, 응답 지연이 잠금을 잡고 있지 않은지."""
import asyncio
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_boss_auction as ba  # noqa: E402


class _Log:
    def info(self, *_):
        pass


class _Resp:
    def __init__(self, delay, record):
        self.delay, self.record = delay, record

    async def edit_message(self, **kw):
        await asyncio.sleep(self.delay)   # 느린 Discord 응답 흉내
        self.record.append(("edit", time.monotonic()))

    async def send_message(self, text, ephemeral=False):
        await asyncio.sleep(self.delay)
        self.record.append(("reject", text))


def _message(mid, refreshed):
    async def edit(**kw):
        refreshed.append(kw["embed"])
    return SimpleNamespace(id=mid, edit=edit)


def _embed_text(embed):
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name} {f.value}" for f in embed.fields]
    return "\n".join(parts)


def _interaction(uid, mid, record, delay, message=None, created_at=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"u{uid}"),
        message=message or SimpleNamespace(id=mid, edit=None),
        created_at=created_at or datetime.now(timezone.utc),
        response=_Resp(delay, record))


def _bid_button(view):
    return next(c for c in view.children if getattr(c, "custom_id", "") == "boss_board_bid")


def test_burst_bids_board():
    async def run():
        d = tempfile.mkdtemp()
        a = ba.BossAuction(None, channel_id=1, data_dir=d, is_admin=lambda u: True, logger=_Log())
        a._schedule_board = lambda *x: None
        ba.BID_SETTLE_SEC = 0.5
        lot = ba.make_lot("집행검", "", 100_000, 10_000, 30)
        board = {"type": "board", "status": "active", "current": None, "lots": [lot], "source_message_id": None}
        ba.board_start_next(board, time.time() - 1)
        a.store.save(1, board)
        view = ba.BoardView(a, state="active")
        record, refreshed = [], []
        msg = _message(1, refreshed)
        # 3명이 동시에 20번 (같은 사람 연속 클릭 포함), 각 응답 0.2초 걸림
        uids = [1, 2, 2, 3, 1, 1, 2, 3, 3, 1, 2, 1, 3, 2, 2, 1, 3, 1, 2, 3]
        t0 = time.monotonic()
        await asyncio.gather(*[view.bid_btn.callback(_interaction(u, 1, record, 0.2, msg)) for u in uids])
        took = time.monotonic() - t0
        lot = a.store.get(1)["lots"][0]
        edits = [r for r in record if r[0] == "edit"]
        rejects = [r for r in record if r[0] == "reject"]
        assert len(edits) + len(rejects) == len(uids)          # 모든 클릭에 응답
        assert lot["bid_count"] == len(edits) == 1              # 잠금 동안 동시 클릭은 첫 입찰 하나만
        assert all("먼저 들어왔어요" in r[1] or "최고 입찰자" in r[1] for r in rejects)
        assert took < 1.0, took                                  # 응답이 잠금 밖이라 직렬로 쌓이지 않음
        # 잠금 중 화면은 입찰 버튼이 꺼져 있고, 잠금이 풀리면 한 번 다시 그려 버튼을 연다
        assert _bid_button(a.view_for(a.store.get(1))).disabled
        await asyncio.sleep(0.8)
        assert len(refreshed) == 1, len(refreshed)
        assert f"u{lot['bidder_id']}" in _embed_text(refreshed[0]), _embed_text(refreshed[0])
        assert not _bid_button(a.view_for(a.store.get(1))).disabled
        # 잠금이 풀린 뒤 다른 사람 클릭은 정상 입찰
        await view.bid_btn.callback(_interaction(2 if lot["bidder_id"] != "2" else 3, 1, record, 0.0, msg))
        assert a.store.get(1)["lots"][0]["bid_count"] == 2
        saved = ba.AuctionStore(os.path.join(d, "boss_auction_state.json")).get(1)["lots"][0]
        assert saved["bid_count"] == 2                           # 파일에도 마지막 입찰까지 저장

    asyncio.run(run())


def _new_board(a, countdown=30):
    lot = ba.make_lot("집행검", "", 100_000, 10_000, countdown)
    board = {"type": "board", "status": "active", "current": None, "lots": [lot], "source_message_id": None}
    ba.board_start_next(board, time.time() - 1)
    a.store.save(1, board)


def _check_consistent(lot):
    """가격·최고 입찰자·히스토리가 서로 맞는지, 잠금 중에 두 건이 들어가지 않았는지."""
    hist = lot["bid_history"]
    assert lot["bid_count"] == len(hist)
    assert [h["price"] for h in hist] == [100_000 + 10_000 * i for i in range(len(hist))]
    assert lot["current_price"] == hist[-1]["price"]
    assert lot["bidder_id"] == hist[-1]["uid"]                          # 최고 입찰자 = 마지막 입찰
    for prev, cur in zip(hist, hist[1:]):
        assert cur["uid"] != prev["uid"]                                  # 같은 사람 연속 입찰 없음
        assert cur["ts"] >= prev["ts"] + ba.BID_SETTLE_SEC - 1e-6         # 잠금 시간 안에 두 번째 입찰 없음


def test_burst_300_at_once():
    """30명이 같은 순간 300번 클릭 (응답 0.05~0.4초 무작위)."""
    async def run():
        random.seed(1)
        d = tempfile.mkdtemp()
        a = ba.BossAuction(None, channel_id=1, data_dir=d, is_admin=lambda u: True, logger=_Log())
        a._schedule_board = lambda *x: None
        ba.BID_SETTLE_SEC = 0.5
        _new_board(a)
        view = ba.BoardView(a, state="active")
        record, refreshed = [], []
        msg = _message(1, refreshed)
        uids = [random.randint(1, 30) for _ in range(300)]
        t0 = time.monotonic()
        await asyncio.gather(*[view.bid_btn.callback(
            _interaction(u, 1, record, random.uniform(0.05, 0.4), msg)) for u in uids])
        took = time.monotonic() - t0
        lot = a.store.get(1)["lots"][0]
        assert len(record) == 300                                         # 300번 모두 응답
        assert lot["bid_count"] == 1                                      # 동시 클릭은 한 건만
        _check_consistent(lot)
        assert took < 1.0, took
        await asyncio.sleep(0.8)
        assert len(refreshed) == 1 and f"u{lot['bidder_id']}" in _embed_text(refreshed[0])
        saved = ba.AuctionStore(os.path.join(d, "boss_auction_state.json")).get(1)["lots"][0]
        assert saved["bid_count"] == 1
        return took

    return asyncio.run(run())


def test_burst_300_over_time():
    """30명이 3초 동안 300번 클릭 — 네트워크 지연 0~0.3초로 도착 순서가 클릭 순서와 뒤섞임."""
    async def run():
        random.seed(2)
        d = tempfile.mkdtemp()
        a = ba.BossAuction(None, channel_id=1, data_dir=d, is_admin=lambda u: True, logger=_Log())
        a._schedule_board = lambda *x: None
        ba.BID_SETTLE_SEC = 0.5
        _new_board(a)
        view = ba.BoardView(a, state="active")
        record, refreshed, waits = [], [], []
        msg = _message(1, refreshed)
        start = time.time()

        async def click(at, uid):
            await asyncio.sleep(at)
            net = random.uniform(0.0, 0.3)
            pressed = datetime.fromtimestamp(time.time() - net, timezone.utc)   # 누른 시각은 도착보다 이르다
            t = time.monotonic()
            await view.bid_btn.callback(_interaction(uid, 1, record, 0.1, msg, created_at=pressed))
            waits.append(time.monotonic() - t - 0.1)                       # Discord 응답 시간을 뺀 처리 시간

        await asyncio.gather(*[click(random.uniform(0, 3.0), random.randint(1, 30)) for _ in range(300)])
        lot = a.store.get(1)["lots"][0]
        assert len(record) == 300
        _check_consistent(lot)
        assert 3 <= lot["bid_count"] <= 7, lot["bid_count"]               # 3초 / 0.5초 잠금 → 대략 6건
        waits.sort()
        assert waits[int(len(waits) * 0.99)] < 0.05, waits[-5:]           # 99% 클릭이 50ms 안에 판정
        await asyncio.sleep(0.8)
        assert f"u{lot['bidder_id']}" in _embed_text(refreshed[-1])       # 마지막 화면 = 실제 최고 입찰자
        return lot["bid_count"], time.time() - start, waits[-1]

    return asyncio.run(run())


if __name__ == "__main__":
    test_burst_bids_board()
    print("✓ test_burst_bids_board")
    print(f"✓ test_burst_300_at_once ({test_burst_300_at_once():.2f}s)")
    n, took, worst = test_burst_300_over_time()
    print(f"✓ test_burst_300_over_time (입찰 {n}건 / {took:.1f}s, 최장 처리 {worst * 1000:.1f}ms)")
