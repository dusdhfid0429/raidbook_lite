# -*- coding: utf-8 -*-
"""진행자 ⏱️ 경매 마감 + 동시 입찰 폭주 — 카운트 중 몰린 입찰, 마감 버튼 연타, 무입찰 마감과 첫 입찰의 경합.

검증하는 불변식
- 모든 클릭에 응답하고, 잠금 시간 안에 두 건이 들어가지 않으며, 최고 입찰자 = 마지막 입찰
- 카운트 중 입찰이 들어오면 카운트가 정확히 한 번 취소되고 경매는 계속 (음성 취소 안내도 한 번)
- 마감 버튼을 여러 명이 동시에 눌러도 카운트는 하나만 시작
- 마감된 뒤(카운트 끝난 뒤)에 누른 입찰은 받지 않고, 카운트가 끝나기 전에 누른 입찰은 마감 때문에 거부되지 않음
- 마감을 누르기 전 일반 진행 중 폭주에도 자동 마감·카운트가 생기지 않음
- 입찰에 성공한 사람마다 본인에게만 확인 메시지 (거부된 사람은 거부 안내)
- 입찰 0건에서 마감과 첫 입찰이 동시에 오면 '유찰' 과 '입찰 접수' 중 하나만 일어남
"""
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

CLOSED_REJECTS = ("경매가 끝났어요", "카운트가 끝난 뒤")


class _Log:
    def info(self, *_):
        pass


class _Followup:
    def __init__(self, record):
        self.record = record

    async def send(self, text, ephemeral=False):
        self.record.append(("followup", text))


class _Resp:
    def __init__(self, delay, record):
        self.delay, self.record = delay, record

    async def edit_message(self, **kw):
        await asyncio.sleep(self.delay)
        self.record.append(("edit", None))

    async def send_message(self, text, ephemeral=False):
        await asyncio.sleep(self.delay)
        self.record.append(("reject", text))

    async def defer(self, **kw):
        await asyncio.sleep(self.delay)


def _interaction(uid, record, delay, pressed=None):
    async def _edit_original(**kw):
        record.append(("edit", None))

    async def _msg_edit(**kw):
        pass

    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"u{uid}"),
        message=SimpleNamespace(id=1, edit=_msg_edit),
        created_at=pressed or datetime.now(timezone.utc),
        response=_Resp(delay, record),
        followup=_Followup(record),
        edit_original_response=_edit_original)


def _pressed(net):
    """Discord 기준 누른 시각 — 도착(지금)보다 net 초 이르다."""
    return datetime.fromtimestamp(time.time() - net, timezone.utc)


def _setup(countdown=30, settle=0.5):
    ba.BID_SETTLE_SEC = settle
    a = ba.BossAuction(None, channel_id=1, data_dir=tempfile.mkdtemp(), is_admin=lambda u: True, logger=_Log())
    a.manual_close = True
    a.tts_port = 1
    voice = []

    async def _fake_tts(cmd, args):
        voice.append((cmd, args))

    a._tts = _fake_tts
    lot = ba.make_lot("집행검", "", 100_000, 10_000, countdown)
    board = {"type": "board", "status": "active", "current": None, "lots": [lot], "source_message_id": None}
    ba.board_start_next(board, time.time() - 1, manual=True)
    a.store.save(1, board)
    return a, ba.BoardView(a, state="active"), voice


def _lot(a):
    return ba.board_current(a.store.get(1)) or a.store.get(1)["lots"][0]


def _answers(record):
    return [r for r in record if r[0] in ("edit", "reject")]


def _success_notes(record):
    return [r[1] for r in record if r[0] == "followup" and "입찰 완료" in r[1]]


def _check_consistent(lot):
    hist = lot.get("bid_history") or []
    assert lot.get("bid_count", 0) == len(hist)
    assert [h["price"] for h in hist] == [100_000 + 10_000 * i for i in range(len(hist))]
    if hist:
        assert lot["current_price"] == hist[-1]["price"] and lot["bidder_id"] == hist[-1]["uid"]
    for prev, cur in zip(hist, hist[1:]):
        assert cur["uid"] != prev["uid"]
        assert cur["ts"] >= prev["ts"] + ba.BID_SETTLE_SEC - 1e-6


async def _cleanup(a):
    await asyncio.sleep(0)
    for t in list(a._close_tasks.values()) + list(a._tick_tasks.values()) + list(a._refresh_tasks.values()):
        t.cancel()


def test_counting_then_300_at_once():
    """카운트 중 30명이 같은 순간 300번 입찰 → 한 건만 접수, 카운트 한 번 취소, 경매 계속."""
    async def run():
        random.seed(11)
        a, view, voice = _setup()
        record = []
        await view.bid_btn.callback(_interaction(99, record, 0.0))
        await asyncio.sleep(ba.BID_SETTLE_SEC + 0.05)
        await view.close_btn.callback(_interaction(1000, record, 0.0))
        await asyncio.sleep(0)
        assert _lot(a)["closing_started_at"] and [c for c, _ in voice] == ["countdown"]
        record.clear()
        t0 = time.monotonic()
        await asyncio.gather(*[view.bid_btn.callback(
            _interaction(random.randint(1, 30), record, random.uniform(0.05, 0.4), _pressed(random.uniform(0, 0.2))))
            for _ in range(300)])
        took = time.monotonic() - t0
        await asyncio.sleep(0)
        lot = _lot(a)
        assert len(_answers(record)) == 300
        assert lot["bid_count"] == 2 and sum(1 for r in record if r[0] == "edit") == 1
        notes = _success_notes(record)
        assert len(notes) == 1 and "110,000" in notes[0] and "카운트가 취소" in notes[0]   # 성공한 사람에게만 확인
        assert lot["closing_started_at"] is None and lot["countdown_resets"] == 1 and lot["status"] == "active"
        assert [c for c, _ in voice] == ["countdown", "countdown_cancel"]
        assert not any(any(k in r[1] for k in CLOSED_REJECTS) for r in record if r[0] == "reject")
        assert ba.close_deadline(lot) is None
        _check_consistent(lot)
        assert took < 1.5, took
        await _cleanup(a)

    asyncio.run(run())


def test_close_button_pressed_by_many():
    """운영진 50명이 ⏱️ 마감을 동시에 → 카운트는 하나만 시작."""
    async def run():
        a, view, voice = _setup()
        record = []
        await view.bid_btn.callback(_interaction(99, record, 0.0))
        await asyncio.sleep(ba.BID_SETTLE_SEC + 0.05)
        record.clear()
        await asyncio.gather(*[view.close_btn.callback(_interaction(1000 + i, record, random.uniform(0, 0.2)))
                               for i in range(50)])
        await asyncio.sleep(0)
        assert sum(1 for r in record if r[0] == "edit") == 1
        assert sum(1 for r in record if r[0] == "followup" and "이미" in r[1]) == 49
        assert [c for c, _ in voice] == ["countdown"]
        await _cleanup(a)

    asyncio.run(run())


def test_nobid_close_races_first_bids():
    """입찰 0건에서 ⏱️ 마감과 첫 입찰 300건이 동시에 — 유찰이면 접수 0건, 접수됐으면 경매 계속 (30회 반복)."""
    async def run(seed):
        random.seed(seed)
        a, view, voice = _setup()
        record = []
        calls = [view.bid_btn.callback(_interaction(random.randint(1, 30), record, random.uniform(0, 0.3)))
                 for _ in range(300)]
        calls.insert(random.randint(0, 300), view.close_btn.callback(_interaction(1000, record, 0.0)))
        await asyncio.gather(*calls)
        await asyncio.sleep(0)
        board = a.store.get(1)
        lot = board["lots"][0]
        _check_consistent(lot)
        if lot["status"] == "active":
            assert lot["bid_count"] == 1 and ba.board_state(board) == "active"
        else:
            assert lot["bid_count"] == 0 and not lot.get("bidder_id")      # 유찰 — 입찰 접수 없음
            assert ba.board_state(board) != "active"
            assert ("speak", ["집행검, 유찰", None, 1]) in voice
        await _cleanup(a)
        return lot["status"] == "active"

    outcomes = {asyncio.run(run(s)) for s in range(30)}
    assert outcomes   # 두 결과 모두 불변식을 지키면 된다 (어느 쪽이 나올지는 도착 순서에 달림)


def test_long_session_bid_storm_and_closing():
    """30명이 6초 동안 600번 입찰, 운영진 3명이 0.3초마다 동시에 ⏱️ 마감 — 실제 마감 타이머까지 돌려서 확인."""
    async def run():
        random.seed(7)
        a, view, voice = _setup(countdown=1, settle=0.3)
        record, bid_rejects, starts, waits = [], [], [], []
        start = time.time()

        async def click(at, uid):
            await asyncio.sleep(at)
            net = random.uniform(0.0, 0.3)
            pressed = time.time() - net
            rec = []
            t = time.monotonic()
            await view.bid_btn.callback(_interaction(uid, rec, 0.05, datetime.fromtimestamp(pressed, timezone.utc)))
            waits.append(time.monotonic() - t - 0.05)
            await asyncio.sleep(0.01)   # 본인 확인 메시지는 화면 갱신 뒤 따로 나감
            record.extend(rec)
            bid_rejects.extend((pressed, r[1]) for r in rec if r[0] == "reject")

        async def admin(at, who):
            await asyncio.sleep(at)
            rec = []
            await view.close_btn.callback(_interaction(who, rec, random.uniform(0, 0.1)))
            if any(r[0] == "edit" for r in rec):
                starts.append(time.time())

        jobs = [click(random.uniform(0, 6.0), random.randint(1, 30)) for _ in range(600)]
        jobs += [admin(0.3 * k + random.uniform(0, 0.05), 1000 + i) for k in range(1, 20) for i in range(3)]
        await asyncio.gather(*jobs)
        # 폭주가 끝난 뒤 마지막 마감 → 카운트(1초)+여유 후 자동 낙찰
        await asyncio.sleep(ba.BID_SETTLE_SEC + 0.05)
        final = []
        await view.close_btn.callback(_interaction(2000, final, 0.0))
        assert any(r[0] == "edit" for r in final), final
        await asyncio.sleep(1 + ba.CLOSE_GRACE_SEC + 0.5)
        board = a.store.get(1)
        lot = board["lots"][0]
        assert len(_answers(record)) == 600
        assert len(_success_notes(record)) == lot["bid_count"]
        assert lot["status"] != "active" and ba.board_state(board) != "active"     # 마지막 카운트로 마감됨
        _check_consistent(lot)
        assert lot["bid_count"] >= 5
        cancels = [c for c, _ in voice if c == "countdown_cancel"]
        counts = [c for c, _ in voice if c == "countdown"]
        assert len(cancels) == lot.get("countdown_resets", 0) >= 1                 # 입찰로 취소된 만큼만 취소 안내
        assert len(counts) == len(cancels) + 1                                      # 시작한 카운트 = 취소 + 마지막 1번
        assert ("speak", [f"집행검, u{lot['bidder_id']}님 낙찰!", None, 1]) in voice
        final_deadline = float(lot["closing_started_at"]) + 1
        for pressed, text in bid_rejects:
            if any(k in text for k in CLOSED_REJECTS):
                assert pressed >= final_deadline - 0.01, (pressed - start, text)   # 카운트 전에 누른 입찰은 마감 탓에 안 버림
        assert all(h["ts"] < final_deadline for h in lot["bid_history"])         # 마감 뒤 입찰은 접수 안 됨
        waits.sort()
        assert waits[int(len(waits) * 0.99)] < 0.1, waits[-5:]
        await _cleanup(a)
        return lot["bid_count"], len(cancels), waits[-1]

    return asyncio.run(run())


def test_storm_before_close_pressed():
    """마감을 누르기 전 일반 진행 중: 시작 직후 300명 동시 + 6초간 600번 폭주 — 자동 마감 없이 순서·잠금 유지."""
    async def run():
        random.seed(5)
        a, view, voice = _setup(countdown=5, settle=0.3)
        record, waits = [], []

        async def click(at, uid, delay):
            await asyncio.sleep(at)
            net = random.uniform(0.0, 0.3)
            t = time.monotonic()
            await view.bid_btn.callback(_interaction(uid, record, delay, _pressed(net)))
            waits.append(time.monotonic() - t - delay)

        jobs = [click(0, random.randint(1, 30), random.uniform(0.05, 0.4)) for _ in range(300)]       # 시작 순간 몰림
        jobs += [click(random.uniform(0, 6.0), random.randint(1, 30), 0.05) for _ in range(600)]     # 이어지는 폭주
        await asyncio.gather(*jobs)
        await asyncio.sleep(ba.CLOSE_GRACE_SEC + 0.3)
        lot = _lot(a)
        assert len(_answers(record)) == 900
        assert len(_success_notes(record)) == lot["bid_count"]                  # 성공한 입찰마다 본인 확인 메시지
        assert lot["status"] == "active" and ba.close_deadline(lot) is None      # 진행자가 누르기 전엔 절대 마감 안 됨
        assert not lot.get("closing_started_at") and not lot.get("countdown_resets")
        assert voice == []                                                       # 카운트 관련 음성 없음
        assert not any(any(k in r[1] for k in CLOSED_REJECTS) for r in record if r[0] == "reject")
        _check_consistent(lot)
        assert lot["bid_count"] >= 10                                            # 6초 / 0.3초 잠금 → 대략 20건
        assert sum(1 for r in record if r[0] == "edit") == lot["bid_count"]
        waits.sort()
        assert waits[int(len(waits) * 0.99)] < 0.1, waits[-5:]
        await _cleanup(a)
        return lot["bid_count"], waits[-1]

    return asyncio.run(run())


if __name__ == "__main__":
    test_counting_then_300_at_once()
    print("✓ test_counting_then_300_at_once")
    n, worst = test_storm_before_close_pressed()
    print(f"✓ test_storm_before_close_pressed (입찰 {n}건, 최장 처리 {worst * 1000:.1f}ms)")
    test_close_button_pressed_by_many()
    print("✓ test_close_button_pressed_by_many")
    test_nobid_close_races_first_bids()
    print("✓ test_nobid_close_races_first_bids")
    n, resets, worst = test_long_session_bid_storm_and_closing()
    print(f"✓ test_long_session_bid_storm_and_closing (입찰 {n}건 / 카운트 취소 {resets}회, 최장 처리 {worst * 1000:.1f}ms)")
