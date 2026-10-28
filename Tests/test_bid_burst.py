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


def test_burst_bids_board():
    async def run():
        d = tempfile.mkdtemp()
        a = ba.BossAuction(None, channel_id=1, data_dir=d, is_admin=lambda u: True, logger=_Log())
        a._schedule_board = lambda *x: None
        lot = ba.make_lot("집행검", "", 100_000, 10_000, 30)
        board = {"type": "board", "status": "active", "current": None, "lots": [lot], "source_message_id": None}
        ba.board_start_next(board, time.time() - 1)
        a.store.save(1, board)
        view = ba.BoardView(a, state="active")
        record, refreshed = [], []
        ba.REFRESH_DELAY_SEC = 0.1
        msg = _message(1, refreshed)
        # 3명이 번갈아 20번, 같은 사람 연속 클릭도 섞음 → 각 응답 0.2초 걸림
        uids = [1, 2, 2, 3, 1, 1, 2, 3, 3, 1, 2, 1, 3, 2, 2, 1, 3, 1, 2, 3]
        t0 = time.monotonic()
        await asyncio.gather(*[view.bid_btn.callback(_interaction(u, 1, record, 0.2, msg)) for u in uids])
        took = time.monotonic() - t0
        lot = a.store.get(1)["lots"][0]
        edits = [r for r in record if r[0] == "edit"]
        rejects = [r for r in record if r[0] == "reject"]
        assert len(edits) + len(rejects) == len(uids)          # 모든 클릭에 응답
        assert lot["bid_count"] == len(edits)
        assert lot["current_price"] == 100_000 + 10_000 * (len(edits) - 1)
        assert all("최고 입찰자" in r[1] for r in rejects)        # 거부는 같은 사람 연속 입찰뿐
        assert took < 1.0, took                                  # 응답이 잠금 밖이라 직렬로 쌓이지 않음 (20×0.2=4초 아님)
        # 클릭별 응답이 어떤 순서로 도착해도, 연타가 멈추면 최종 입찰자로 딱 한 번 다시 그린다
        await asyncio.sleep(0.4)
        assert len(refreshed) == 1, len(refreshed)
        assert f"u{lot['bidder_id']}" in _embed_text(refreshed[0]), _embed_text(refreshed[0])
        saved = ba.AuctionStore(os.path.join(d, "boss_auction_state.json")).get(1)["lots"][0]
        assert saved["bid_count"] == lot["bid_count"]            # 파일에도 마지막 입찰까지 저장

    asyncio.run(run())


if __name__ == "__main__":
    test_burst_bids_board()
    print("✓ test_burst_bids_board")
