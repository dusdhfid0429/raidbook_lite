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


def test_board_sequence_and_results():
    lots, _ = ba.parse_board_lots("집행검 100만 10만\n반지 x2", D)
    board = {"lots": lots, "status": "active", "current": None}
    first = ba.board_start_next(board, 1000)
    assert first is lots[0] and first["status"] == "active" and board["current"] == 0
    assert ba.close_deadline(first) == 1005
    assert ba.apply_bid(first, 1, "홍길동", now=1002)[0]
    assert ba.apply_bid(first, 2, "김철수", now=1003)[0]
    assert first["current_price"] == 1_100_000
    assert ba.board_close_current(board)["bidder_name"] == "김철수"
    second = ba.board_start_next(board, 1010)
    assert second is lots[1]
    ba.board_close_current(board)          # 유찰
    third = ba.board_start_next(board, 1020)
    assert third is lots[2]
    third["status"] = "cancelled"          # 종료 버튼 시나리오
    assert ba.board_start_next(board, 1030) is None and board["status"] == "closed"
    assert ba.board_results_text(board) == "집행검 김철수 110만 · 반지 (1/2) 유찰 · 반지 (2/2) 취소"


def test_fmt_amount():
    assert ba.fmt_amount_kr(1_300_000) == "130만"
    assert ba.fmt_amount_kr(150_000_000) == "1억5,000만"
    assert ba.fmt_amount_kr(12_345) == "12,345"


def test_board_embed_builds():
    lots, _ = ba.parse_board_lots("집행검\n반지", D)
    board = {"lots": lots, "status": "active", "current": None, "boss_name": "드레이크", "boss_time": "18:00",
             "source_message_id": "1"}
    ba.board_start_next(board, 2000)
    e = ba.build_board_embed(board, "아데나", now=1990)
    assert "진행판" in e.title and "⏳ 시작" in [f.name for f in e.fields]
    e = ba.build_board_embed(board, "아데나", now=2001)
    assert "⏱️ 마감" in [f.name for f in e.fields]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
