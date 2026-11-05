# -*- coding: utf-8 -*-
"""RaidBook_clan_settle 테스트 — 보탐 경매건별 혈맹 분배 정산."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_clan_settle as cs  # noqa: E402

LOTS = [{"label": "집행검", "winner_name": "투신", "price": 3_000_000},
        {"label": "반지 (1/2)", "winner_name": "바람", "price": 1_000_000},
        {"label": "반지 (2/2)", "winner_name": "하늘", "price": 500_000}]


def _groups(**counts):
    emojis = {"정도": "⚔️", "슈퍼맨": "🦸", "썰자": "🔪"}
    return [(name, emojis.get(name, ""), [f"{name}{i}" for i in range(n)]) for name, n in counts.items()]


def test_multi_items_summed_and_split_by_clan():
    rec = cs.compute(LOTS, 45, _groups(정도=20, 슈퍼맨=15, 썰자=10))
    assert rec["total"] == 4_500_000 and rec["per"] == 100_000
    assert [(c["name"], c["amount"]) for c in rec["clans"]] == [("정도", 2_000_000), ("슈퍼맨", 1_500_000), ("썰자", 1_000_000)]
    assert rec["remainder"] == 0


def test_remainder_kept_separately():
    rec = cs.compute(LOTS + [{"label": "목걸이", "winner_name": "구름", "price": 600_000}], 45,
                     _groups(정도=20, 슈퍼맨=15, 썰자=10))
    assert rec["total"] == 5_100_000 and rec["per"] == 113_333   # 재경매 낙찰 반영
    assert rec["clans"][0]["amount"] == 2_266_660
    assert sum(c["amount"] for c in rec["clans"]) + rec["remainder"] == rec["total"]
    assert rec["remainder"] == 15


def test_no_attendees_raises():
    try:
        cs.compute(LOTS, 0, [])
        raise AssertionError("참석자 0명 통과")
    except ValueError:
        pass


def test_groups_from_split():
    split = ([("정도", "⚔️", ["a", "b"]), ("슈퍼맨", "🦸", [])], ["c"])
    g = cs.clan_groups_from_split(split, ["a", "b", "c"])
    assert g == [("정도", "⚔️", ["a", "b"]), ("슈퍼맨", "🦸", []), ("미지정", "❔", ["c"])]
    assert cs.clan_groups_from_split(([("정도", "⚔️", ["a"])], []), ["a"]) == [("정도", "⚔️", ["a"])]
    assert cs.clan_groups_from_split(None, ["a", "b"]) == [("전체", "👥", ["a", "b"])]   # 혈맹 구분 끔


def test_viewer_clan():
    clans = ["정도", "슈퍼맨", "썰자"]
    assert cs.viewer_clan(["슈퍼맨운영진", "정도"], clans) == "슈퍼맨"   # '<혈맹>운영진'이 먼저
    assert cs.viewer_clan(["운영진", "정도"], clans) == "정도"
    assert cs.viewer_clan(["운영진"], clans) is None


def test_detail_text_and_store():
    rec = cs.compute(LOTS, 45, _groups(정도=20, 슈퍼맨=15, 썰자=10))
    rec.update({"boss_name": "오만의탑 정상", "boss_time": "21:00", "boss_date": "11/07", "lots": LOTS, "unsold": ["목걸이"]})
    store = cs.ClanSettleStore(os.path.join(tempfile.mkdtemp(), "clan_settlements.json"))
    saved = store.save("123", rec)
    assert store.get("123")["total"] == 4_500_000 and saved["created_at"]
    again = store.save("123", dict(rec, total=5_100_000))
    assert again["created_at"] == saved["created_at"] and store.get("123")["total"] == 5_100_000
    text = cs.detail_text(saved, "아데나", viewer="슈퍼맨")
    assert "11/07 21:00 오만의탑 정상" in text and "4,500,000 아데나" in text
    assert "1인당 **100,000**" in text and "🦸 슈퍼맨 15명 → **1,500,000**" in text
    assert "미낙찰 1건 (목걸이)" in text
    assert "슈퍼맨 혈원" in text and "슈퍼맨0" in text and "정도0" not in text   # 다른 혈맹 명단은 안 보임
    assert "혈원" not in cs.detail_text(saved, viewer=None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("ALL OK")
