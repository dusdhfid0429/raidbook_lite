# -*- coding: utf-8 -*-
"""경매 입찰 연타 — 짧은 간격으로 몰린 클릭이 모두 판정되고, 응답 지연이 잠금을 잡고 있지 않은지."""
import asyncio
import os
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


def _interaction(uid, mid, record, delay, message=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"u{uid}"),
        message=message or SimpleNamespace(id=mid, edit=None),
        created_at=datetime.now(timezone.utc),
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


if __name__ == "__main__":
    test_burst_bids_board()
    print("✓ test_burst_bids_board")
