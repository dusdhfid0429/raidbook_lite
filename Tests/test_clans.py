# -*- coding: utf-8 -*-
"""RaidBook_clans 테스트 — 참석자 혈맹별 묶음 표시."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_clans as rbc  # noqa: E402

TWO = [("정도", "⚔️"), ("슈퍼맨", "🦸")]
ROLES = {
    "홍길동": {"정도"}, "김철수": {"정도", "운영진"}, "최강": {"슈퍼맨"},
    "양다리": {"정도", "슈퍼맨"}, "손님": set(),
}


def test_parse_roles():
    assert rbc.parse_clan_roles(None) == [("정도", "⚔️"), ("슈퍼맨", "🦸"), ("썰자", "🔪"), ("방다빼", "🏃"), ("복덕방", "🏠")]
    assert rbc.parse_clan_roles([]) == []
    assert rbc.parse_clan_roles(["A", {"role": "B", "emoji": "🅱"}, "A", ""]) == [("A", ""), ("B", "🅱")]


def test_split_names():
    assert rbc.split_names("없음") == []
    assert rbc.split_names("") == []
    assert rbc.split_names("a, b") == ["a", "b"]


def test_group_and_format():
    clans = TWO
    names = ["홍길동", "최강", "김철수", "양다리", "손님", "모름"]
    groups, unassigned = rbc.group_by_clan(names, clans, lambda n: ROLES.get(n, set()))
    assert groups == [("정도", "⚔️", ["홍길동", "김철수", "양다리 ⚠️"]), ("슈퍼맨", "🦸", ["최강"])], groups
    assert unassigned == ["손님", "모름"]
    block = rbc.format_clan_block("✅ 현재 참석", 6, groups, unassigned)
    assert block == ("✅ 현재 참석: 총 6명\n"
                     "⚔️ 정도 (3명) | 홍길동, 김철수, 양다리 ⚠️\n"
                     "🦸 슈퍼맨 (1명) | 최강\n"
                     "❔ 미지정 (2명) | 손님, 모름"), block
    assert rbc.format_clan_counts(groups, unassigned) == "정도 3 · 슈퍼맨 1 · 미지정 2"


def test_empty_clan_and_no_unassigned():
    clans = TWO
    groups, unassigned = rbc.group_by_clan(["최강"], clans, lambda n: ROLES.get(n, set()))
    block = rbc.format_clan_block("✅ 최종 참석", 1, groups, unassigned)
    assert block == "✅ 최종 참석: 총 1명\n⚔️ 정도 (0명) | 없음\n🦸 슈퍼맨 (1명) | 최강", block


def test_lookup_error_is_unassigned():
    def boom(_):
        raise RuntimeError("x")
    groups, unassigned = rbc.group_by_clan(["a"], rbc.parse_clan_roles(None), boom)
    assert unassigned == ["a"] and all(not g[2] for g in groups)


def test_short_name():
    clans = ("정도", "슈퍼맨")
    cases = {
        "[정도]투신(애마)/기사/90": "투신(애마)", "[정도]레미(애찌)/90/요정": "레미(애찌)",
        "@[슈퍼맨] 군주/벌/82": "군주", "[정도]": "[정도]", "12/34": "12/34", "정도/기사/정도완력": "정도완력",
        "[슈퍼맨]빼쥬 법사 88": "빼쥬", "[슈퍼맨] 쿡 /요정/77": "쿡", "[슈퍼맨] PPF/법사/83": "PPF",
        "[정도]경비(부) 기사 79 부산": "경비(부)", "슈퍼맨/미르/87": "미르", "[정도]단장/요정 /00/여수": "단장",
        "양다리/기사 ⚠️": "양다리 ⚠️", "홍길동": "홍길동",
    }
    for raw, want in cases.items():
        assert rbc.short_name(raw, clans) == want, (raw, rbc.short_name(raw, clans))


def test_fields():
    groups = [("정도", "⚔️", ["[정도]a/기사/90", "b ⚠️"]), ("슈퍼맨", "🦸", [])]
    assert rbc.clan_fields(groups, ["[슈퍼맨]c/법사/83"]) == [
        ("⚔️ 정도 2명", "a · b ⚠️"), ("🦸 슈퍼맨 0명", "없음"), ("❔ 미지정 1명", "c")]


def test_fields_split_long():
    people = [f"사람{i:02d}" + "x" * 40 for i in range(60)]
    fields = rbc.clan_fields([("정도", "⚔️", people)], [])
    assert len(fields) > 1 and fields[0][0] == "⚔️ 정도 60명"
    assert all(n == "\u200b" for n, _ in fields[1:])
    assert all(len(v) <= 1024 for _, v in fields)
    assert sum(len(v.split(" · ")) for _, v in fields) == 60


def test_add_missing_roles():
    old = [{"role": "정도", "emoji": "🗡"}, "슈퍼맨"]
    new, added = rbc.add_missing_roles(old)
    assert added == ["썰자", "방다빼", "복덕방"]
    assert new[:2] == old and [r["role"] for r in new[2:]] == added
    assert rbc.add_missing_roles(new) == (new, [])
    assert rbc.add_missing_roles(None) == (None, [])
    assert rbc.add_missing_roles([]) [1] == ["정도", "슈퍼맨", "썰자", "방다빼", "복덕방"]


def test_convert_legacy_description():
    one = "⏰ 리젠 21:00\n\n✅ 최종 참석: 3명 | [정도]a/기사/90, b, c\n\n🎁 **획득 아이템**: 반지"
    desc, names = rbc.convert_legacy_description(one)
    assert desc == "⏰ 리젠 21:00\n\n✅ 최종 참석: 총 3명\n\n🎁 **획득 아이템**: 반지", desc
    assert names == ["[정도]a/기사/90", "b", "c"]
    v30 = ("⏰ 리젠 21:00\n\n✅ 최종 참석: 총 4명\n⚔️ 정도 (2명) | a, b ⚠️\n🦸 슈퍼맨 (0명) | 없음\n"
           "❔ 미지정 (2명) | c, d")
    desc, names = rbc.convert_legacy_description(v30)
    assert desc == "⏰ 리젠 21:00\n\n✅ 최종 참석: 총 4명", desc
    assert names == ["a", "b", "c", "d"], names
    assert rbc.convert_legacy_description("✅ 현재 참석: 0명 | 없음") == ("✅ 현재 참석: 총 0명", [])
    new = "⏰ 리젠 21:00\n\n✅ 최종 참석: 총 4명\n\n🎁 **획득 아이템**: 반지"
    assert rbc.convert_legacy_description(new) is None
    assert rbc.convert_legacy_description("") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
