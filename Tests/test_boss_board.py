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
    assert "⏱️ 마감" in [f.name for f in e.fields]


def _finish(board, winners):
    """lots 를 순서대로 시작·마감. winners: 순번 → (uid, 이름, 가격) 또는 None(유찰)."""
    for i, w in enumerate(winners):
        ba.board_start_next(board, 1000 + i)
        lot = ba.board_current(board)
        if w:
            lot.update(bidder_id=w[0], bidder_name=w[1], current_price=w[2], bid_count=1)
        ba.board_close_current(board)
        ba.board_after_close(board)


def test_relist_and_merge():
    lots, _ = ba.parse_board_lots("집행검\n반지 x2 30만 5만 10초", D)
    first = {"lots": lots, "status": "active", "current": None, "created_at": "2026-10-28T10:00:00"}
    _finish(first, [("1", "김철수", 1_100_000), ("2", "이영희", 300_000), None])
    assert first["status"] == "closed"
    assert ba.board_can_relist(first)
    text = ba.relist_text(first)
    assert text == "반지 (2/2) 30만 5만 10초"
    # 시작가·호가·카운트를 고쳐 다시 올림 — 순번이 이름에 남아 결과가 겹쳐 쓰임
    relots, _ = ba.parse_board_lots("반지 (2/2) 20만 2만 7초", D)
    assert (ba.lot_label(relots[0]), relots[0]["start_price"], relots[0]["increment"], relots[0]["countdown_sec"]) \
        == ("반지 (2/2)", 200_000, 20_000, 7)
    second = {"lots": relots, "status": "active", "current": None, "created_at": "2026-10-28T10:05:00"}
    first["relisted_to"] = "2"
    assert not ba.board_can_relist(first)
    _finish(second, [("3", "박민수", 200_000)])
    merged = ba.lots_results_text(ba.merge_board_lots([first, second]))
    assert merged == "집행검 김철수 110만 · 반지 (1/2) 이영희 30만 · 반지 (2/2) 박민수 20만"
    assert not ba.board_can_relist(second)


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
