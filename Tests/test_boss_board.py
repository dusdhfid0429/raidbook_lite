# -*- coding: utf-8 -*-
"""경매 진행판 순수 로직 테스트 — 입력 파싱·순서 진행·결과 요약."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_boss_auction as ba  # noqa: E402

D = {"start_price": 100_000, "increment": 10_000, "countdown_sec": 5}


def test_parse_line_full_and_defaults():
    assert ba.parse_lot_line("집행검 100만 10만", D) == ("집행검", 1, 1_000_000, 100_000, 5)
    assert ba.parse_lot_line("반지 x2 30만 5만 10초", D) == ("반지", 2, 300_000, 50_000, 10)
    assert ba.parse_lot_line("목걸이", D) == ("목걸이", 1, 100_000, 10_000, 5)
    assert ba.parse_lot_line("반지x3", D)[:2] == ("반지", 3)
    assert ba.parse_lot_line("엘릭서 3개 5만", D) == ("엘릭서", 3, 50_000, 10_000, 5)
    # 이름 끝 작은 숫자는 금액으로 보지 않음
    assert ba.parse_lot_line("룬 3", D)[0] == "룬 3"
    assert ba.parse_lot_line("드래곤 슬레이어 1.5억 1000만", D) == ("드래곤 슬레이어", 1, 150_000_000, 10_000_000, 5)



def test_parse_line_loose_formats():
    # '초' 빠진 카운트, 쉼표·슬래시 구분, 말머리, 천 단위 쉼표
    want = ("집행검", 1, 1_000_000, 100_000, 7)
    for line in ("집행검 100만 10만 7", "집행검 100만,10만,7초", "집행검 100만, 10만, 7",
                 "집행검 100만/10만/7초", "집행검 1,000,000 100,000 7",
                 "집행검 시작가 100만 호가 10만 카운트 7", "집행검 시작가:100만 호가:10만 7초"):
        assert ba.parse_lot_line(line, D) == want, line
    assert ba.parse_lot_line("집행검 100 10 7", D) == ("집행검", 1, 100, 10, 7)
    assert ba.parse_lot_line("집행검 100만 7", D) == ("집행검", 1, 1_000_000, 10_000, 7)
    assert ba.parse_lot_line("룬 3 20만 2만 7초", D) == ("룬 3", 1, 200_000, 20_000, 7)
    assert ba.parse_board_defaults("20만 2만 7", D) == {"start_price": 200_000, "increment": 20_000, "countdown_sec": 7}


def test_parse_line_errors():
    for bad in ("반지 x50", "반지 0초"):
        try:
            ba.parse_lot_line(bad, D)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_defaults():
    assert ba.parse_board_defaults("20만 2만 7초") == {"start_price": 200_000, "increment": 20_000, "countdown_sec": 7}
    assert ba.parse_board_defaults("") == D
    assert ba.format_board_defaults(D) == "10만 1만 5초"


def test_lots_expand_quantity():
    lots, names = ba.parse_board_lots("집행검 100만 10만\n반지 x2\n\n", D)
    assert names == ["집행검", "반지 x2"]
    assert [ba.lot_label(x) for x in lots] == ["집행검", "반지 (1/2)", "반지 (2/2)"]


def test_board_manual_sequence_and_results():
    lots, _ = ba.parse_board_lots("집행검 100만 10만\n반지 x2", D)
    board = {"lots": lots, "status": "active", "current": None}
    assert ba.board_state(board) == "waiting"          # 게시 직후 = 관리자 ▶️ 대기
    first = ba.board_start_next(board, 1000)
    assert first is lots[0] and ba.board_state(board) == "active"
    assert ba.close_deadline(first) == 1005
    assert ba.apply_bid(first, 1, "홍길동", now=1002)[0]
    assert ba.apply_bid(first, 2, "김철수", now=1003)[0]
    assert first["current_price"] == 1_100_000
    assert ba.board_close_current(board)["bidder_name"] == "김철수"
    assert ba.board_after_close(board) is False
    assert ba.board_state(board) == "waiting"          # 다음 아이템은 자동 시작 안 함
    assert lots[1]["status"] == "pending"
    ba.board_start_next(board, 1010)
    ba.board_close_current(board)                      # 유찰
    ba.board_after_close(board)
    lots[2]["status"] = "cancelled"                    # 종료 버튼 시나리오
    assert ba.board_after_close(board) is True and ba.board_state(board) == "closed"
    assert ba.board_results_text(board) == "집행검 김철수 110만 · 반지 (1/2) 유찰 · 반지 (2/2) 취소"


def test_fmt_amount():
    assert ba.fmt_amount_kr(1_300_000) == "130만"
    assert ba.fmt_amount_kr(150_000_000) == "1억5,000만"
    assert ba.fmt_amount_kr(12_345) == "12,345"


def test_board_embed_builds():
    lots, _ = ba.parse_board_lots("집행검\n반지", D)
    board = {"lots": lots, "status": "active", "current": None, "boss_name": "드레이크", "boss_time": "18:00",
             "source_message_id": "1"}
    e = ba.build_board_embed(board, "아데나", now=1990)
    assert "⏸️ 다음 경매" in [f.name for f in e.fields]
    ba.board_start_next(board, 2000)
    e = ba.build_board_embed(board, "아데나", now=2001)
    assert "## ⏱️ 남은 시간 " in e.description   # 카운트는 설명란 큰 글씨 (남은 초를 직접 표시)
    lot = ba.board_current(board)
    ba.apply_bid(lot, 1, "김", now=2001.5)
    e = ba.build_board_embed(board, "아데나", now=2002)
    assert "## 🥇 김 (" in e.description                                   # 최고 입찰자 + 누른 시각
    assert "## ⏳ 입찰 확인 중" in e.description                             # 입찰 직후 대기 중
    dl = 2001.5 + ba.BID_SETTLE_SEC + lot["countdown_sec"]
    e = ba.build_board_embed(board, "아데나", now=dl - 3.2)
    assert "## ⏱️ 남은 시간 4초" in e.description                         # 대기 후 카운트 (올림 초)
    assert ba.fmt_remaining(75) == "1분 15초" and ba.fmt_remaining(0) == "마감 처리 중…"


def _finish(board, winners):
    """lots 를 순서대로 시작·마감. winners: 순번 → (uid, 이름, 가격) 또는 None(유찰)."""
    for i, w in enumerate(winners):
        ba.board_start_next(board, 1000 + i)
        lot = ba.board_current(board)
        if w:
            lot.update(bidder_id=w[0], bidder_name=w[1], current_price=w[2], bid_count=1)
        ba.board_close_current(board)
        ba.board_after_close(board)


def test_relist_options():
    # 3개 중 유찰·낙찰·유찰 → 재경매 목록은 1번·3번, 하나씩 다시 올리면 목록에서 빠짐
    lots, _ = ba.parse_board_lots("집행검 50만 5만\n반지\n목걸이 30만 3만 7초", D)
    first = {"lots": lots, "status": "active", "current": None, "source_message_id": "9", "created_at": "1"}
    _finish(first, [None, ("1", "김", 100_000), None])
    assert ba.board_can_relist(first)
    opts = ba.relist_options(first, [first])
    assert [o["label"] for o in opts] == ["집행검", "목걸이"]
    assert opts[1]["lot"]["start_price"] == 300_000 and "30만" in opts[1]["note"]
    again = {"lots": [ba.make_lot("집행검", "", 40_0000, 4_0000, 5)], "status": "active", "current": None,
             "source_message_id": "9", "created_at": "2"}
    assert [o["label"] for o in ba.relist_options(first, [first, again])] == ["목걸이"]
    _finish(again, [("2", "이", 400_000)])
    assert [o["label"] for o in ba.relist_options(first, [first, again])] == ["목걸이"]
    assert ba.lots_results_text(ba.merge_board_lots([first, again])) == "집행검 이 40만 · 반지 김 10만 · 목걸이 유찰"


def test_auction_options_from_loot():
    assert ba.expand_loot_units("집행검, 반지 x2\n엘릭서 3개") == [
        ("집행검", ""), ("반지", "1/2"), ("반지", "2/2"), ("엘릭서", "1/3"), ("엘릭서", "2/3"), ("엘릭서", "3/3")]
    loot = "집행검, 반지 x2, 목걸이"
    assert [o["label"] for o in ba.auction_options(loot, [])] == ["집행검", "반지 (1/2)", "반지 (2/2)", "목걸이"]
    # 집행검 낙찰, 반지(1/2) 유찰, 반지(2/2) 대기 중 → 목록: 반지(1/2)(유찰), 목걸이
    b1 = {"lots": [ba.make_lot("집행검", "", 100, 10, 5)], "status": "active", "current": None, "created_at": "1"}
    _finish(b1, [("1", "김", 100)])
    b2 = {"lots": [ba.make_lot("반지", "1/2", 100, 10, 5)], "status": "active", "current": None, "created_at": "2"}
    _finish(b2, [None])
    b3 = {"lots": [ba.make_lot("반지", "2/2", 100, 10, 5)], "status": "active", "current": None, "created_at": "3"}
    opts = ba.auction_options(loot, [b1, b2, b3])
    assert [(o["label"], o["note"]) for o in opts] == [("반지 (1/2)", "유찰 — 다시 경매"), ("목걸이", "경매 전")]
    assert ba.lots_results_text(ba.merge_board_lots([b1, b2, b3])) == "집행검 김 100 · 반지 (1/2) 유찰"


def test_pending_entries_and_embed():
    from datetime import datetime, timedelta
    old_mid = str(((int((datetime.now() - timedelta(days=10)).timestamp() * 1000) - ba.DISCORD_EPOCH_MS) << 22))
    new_mid = str(((int(datetime.now().timestamp() * 1000) - ba.DISCORD_EPOCH_MS) << 22))
    assert abs((ba.snowflake_time(new_mid) - datetime.now()).total_seconds()) < 5
    loot = {new_mid: {"boss_name": "드레이크", "boss_time": "18:00", "loot_text": "집행검, 반지 x2, 엘릭서"},
            old_mid: {"boss_name": "카스파", "boss_time": "17:00", "loot_text": "목걸이"},
            "1": {"boss_name": "빈칸", "boss_time": "", "loot_text": ""}}
    sold = {"lots": [ba.make_lot("집행검", "", 100, 10, 5)], "status": "active", "current": None, "created_at": "1"}
    _finish(sold, [("1", "김", 100)])
    flop = {"lots": [ba.make_lot("반지", "1/2", 100, 10, 5)], "status": "active", "current": None, "created_at": "2"}
    _finish(flop, [None])
    running = {"lots": [ba.make_lot("목걸이", "", 100, 10, 5)], "status": "active", "current": None, "created_at": "3"}
    ba.board_start_next(running, 0)
    entries = ba.pending_entries(loot, {new_mid: [sold, flop], old_mid: [running]}, {new_mid: ["엘릭서"]})
    assert len(entries) == 1                                              # 경매장에 올라간 목걸이는 목록에 없음
    new = next(e for e in entries if e["mid"] == new_mid)
    assert [(o["label"], o["note"]) for o in new["options"]] == [
        ("반지 (1/2)", "유찰 — 다시 경매"), ("반지 (2/2)", "경매 전")]  # 낙찰된 집행검·제외한 엘릭서 없음
    pairs = ba.pending_items(entries)
    assert [ba.pending_status(o) for _, o in pairs] == ["유찰", "경매 전"]   # 상태순
    assert [o["label"] for _, o in ba.pending_items(entries, selectable=True)] == ["반지 (1/2)", "반지 (2/2)"]
    e = ba.build_pending_embed(entries, alert_link=lambda m: f"https://x/{m}")
    d = e.description
    assert d.index("⚪ 유찰") < d.index("⏳ 경매 전") and "진행 중" not in d
    assert "반지 (1/2) — [드레이크 18:00" in d and "목걸이" not in d
    old_only = ba.pending_entries(loot, {}, {new_mid: ["집행검", "반지 (1/2)", "반지 (2/2)", "엘릭서"]})
    assert [o["label"] for e in old_only for o in e["options"]] == ["목걸이"]   # 기간 제한 없음 — 10일 전 알림도 남음
    assert "김" not in d and "100" not in d                               # 공개 목록엔 이름·상태만


def test_bid_history_and_deadline_order():
    lot = ba.make_lot("집행검", "", 100, 10, 30)
    lot["status"] = "active"
    lot["started_ts"] = 1000.0
    for i, (uid, ts) in enumerate([(1, 1001.0), (2, 1003.0), (3, 1002.0), (1, 1004.0), (2, 1005.0), (3, 1006.0)]):
        assert ba.apply_bid(lot, uid, f"u{uid}", now=ts)[0]
        if i == 2:
            assert lot["last_bid_at"] == 1003.0   # 늦게 처리된 앞 클릭이 마감을 앞당기지 않음
    text = ba.bid_history_text(lot)
    rows = text.split("\n")
    assert len(rows) == 5 and rows[0].startswith("🥇 u3 (") and rows[0].endswith("— 150")   # 최신 5건, 최고 입찰자가 맨 위
    assert not rows[-1].endswith("— 100")                                 # 6건 중 가장 오래된 건 빠짐
    board = {"lots": [lot], "status": "active", "current": 0}
    e = ba.build_board_embed(board, now=1006.5)
    assert any(f.name.startswith("📜 최근 입찰") for f in e.fields)


def test_settlement_compute_and_button():
    import asyncio
    import tempfile

    board = {"type": "board", "lots": [ba.make_lot("집행검", "", 100, 10, 5), ba.make_lot("반지", "", 100, 10, 5)],
             "status": "active", "current": None, "source_message_id": "555", "boss_name": "카스파", "boss_time": "18:00"}
    _finish(board, [("1", "김", 1_000_003), None])          # 집행검 낙찰, 반지 유찰
    rec = ba.compute_settlement(board, ["1", "2", "3", "2"], {"guild_fee_pct": 10, "operations_pct": 30, "attendee_pct": 60})
    assert rec["total"] == 1_000_003 and rec["attendee_count"] == 3   # 중복 참석자 1번만
    assert rec["operations"] == 300_000 and rec["attendee_pool"] == 600_001 and rec["per_attendee"] == 200_000
    assert rec["guild_fee"] + rec["operations"] + rec["per_attendee"] * 3 == rec["total"]   # 자투리는 혈비로
    assert [x["label"] for x in rec["lots"]] == ["집행검"]                                   # 유찰은 정산 제외
    try:
        ba.compute_settlement(board, ["1"], {"guild_fee_pct": 10, "operations_pct": 30, "attendee_pct": 50})
        raise AssertionError("합 90% 통과")
    except ValueError:
        pass

    class _Log:
        def info(self, *_):
            pass

    a = ba.BossAuction(None, channel_id=1, data_dir=tempfile.mkdtemp(), is_admin=lambda u: True, logger=_Log(),
                       get_attendees=lambda src: ["1", "2", "3"] if src == "555" else [])
    ids = lambda v: [getattr(c, "custom_id", "") for c in v.children]
    flop = {"type": "board", "lots": [ba.make_lot("목걸이", "", 100, 10, 5)], "status": "active", "current": None}
    _finish(flop, [None])
    running = {"type": "board", "lots": [ba.make_lot("귀걸이", "", 100, 10, 5)], "status": "active", "current": None}
    ba.board_start_next(running, 0)
    assert "boss_board_settle" not in ids(a.view_for(flop))       # 유찰만 → 숨김
    assert "boss_board_settle" not in ids(a.view_for(running))    # 진행 중 → 숨김
    assert "boss_board_settle" in ids(a.view_for(board))          # 낙찰 있음 → 노출
    assert "boss_board_settle" in ids(ba.BoardView(a))            # 재시작 복구 등록용엔 항상 있음
    d = ba.compute_settlement(board, ["1", "2", "3"], None)       # 기본 비율 혈비 30 / 운영진 0 / 참석자 70
    assert d["operations"] == 0 and d["attendee_pool"] == 700_002 and d["per_attendee"] == 233_334
    assert d["guild_fee"] == 1_000_003 - 233_334 * 3
    a.store.save(9, board)
    a.store.save(8, flop)

    async def run():
        r, err, already = await a.settle_board(9, by="관리자")
        assert err is None and not already
        r2, err2, already2 = await a.settle_board(9, by="관리자")
        assert already2 and r2["created_at"] == r["created_at"]   # 두 번 눌러도 한 번만 생성
        _, err3, _ = await a.settle_board(8)
        assert err3                                                 # 유찰 진행판은 정산 불가
        return r

    r = asyncio.run(run())
    assert r["attendee_count"] == 3 and r["guild_fee"] + r["operations"] + r["per_attendee"] * 3 == r["total"]
    assert a.settlements.get(9)["board_message_id"] == "9"
    btn = next(c for c in a.view_for(a.store.get(9)).children if getattr(c, "custom_id", "") == "boss_board_settle")
    assert btn.label == "정산 완료"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
