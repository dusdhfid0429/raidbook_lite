# -*- coding: utf-8 -*-
"""RaidBook_boss_auction 순수 로직 테스트: python -m pytest Tests/test_boss_auction.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_boss_auction as ba


def _rec(**kw):
    rec = {"item": "검", "countdown_sec": 5, "start_price": 800000, "increment": 100000,
           "current_price": 0, "bidder_id": None, "bidder_name": "", "bid_count": 0,
           "last_bid_at": None, "status": "active", "started_ts": 1000.0}
    rec.update(kw)
    return rec


def test_parse_amount():
    assert ba.parse_amount_kr("80만") == 800000
    assert ba.parse_amount_kr("1.5억") == 150000000
    assert ba.parse_amount_kr("3,750,000") == 3750000
    assert ba.parse_amount_kr("abc") is None


def test_bid_sequence():
    rec = _rec()
    assert ba.apply_bid(rec, 1, "A", now=1001)[0]
    assert rec["current_price"] == 800000            # 첫 입찰 = 시작가
    assert not ba.apply_bid(rec, 1, "A", now=1002)[0]  # 연속 입찰 거부
    assert ba.apply_bid(rec, 2, "B", now=1003)[0]
    assert rec["current_price"] == 900000 and rec["bidder_name"] == "B" and rec["bid_count"] == 2


def test_deadline_resets_on_bid():
    rec = _rec()
    assert ba.close_deadline(rec) == 1005           # 시작 후 카운트
    ba.apply_bid(rec, 1, "A", now=1004)
    assert ba.close_deadline(rec) == 1009           # 마지막 입찰 후 카운트
    rec["status"] = "closed"
    assert ba.close_deadline(rec) is None


def test_bid_rejected_when_not_active():
    assert not ba.apply_bid(_rec(status="ready"), 1, "A")[0]
