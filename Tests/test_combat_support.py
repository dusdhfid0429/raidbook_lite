# -*- coding: utf-8 -*-
"""RaidBook_combat_support 테스트 — 설정 자리, 혈맹→전투방/대화방, 참석자 병합, 저장, 화면."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_clans as rbc  # noqa: E402
import RaidBook_combat_support as cs  # noqa: E402

CLANS = [("정도", "⚔️"), ("슈퍼맨", "🦸"), ("썰자", "🔪")]


def test_ensure_section_adds_placeholders_once():
    config = {}
    assert cs.ensure_section(config, CLANS) is True
    assert config[cs.ALERT_CHANNEL_KEY] is None
    sec = config["combat_support"]
    assert sec["max_length"] == 15
    assert set(sec["clans"]) == {"정도", "슈퍼맨", "썰자"}
    assert sec["clans"]["정도"] == {"combat_room_channel_id": None}
    assert sec["tts_channel_ids"] == []
    assert cs.ensure_section(config, CLANS) is False
    # 운영진이 넣은 값은 유지, 새 혈맹만 추가
    sec["clans"]["정도"]["combat_room_channel_id"] = 111
    assert cs.ensure_section(config, CLANS + [("복덕방", "🏠")]) is True
    assert sec["clans"]["정도"]["combat_room_channel_id"] == 111
    assert "복덕방" in sec["clans"]


def test_settings_rooms_and_tts():
    config = {"combat_alert_channel_id": "999"}
    cs.ensure_section(config, CLANS)
    clans = config["combat_support"]["clans"]
    clans["정도"].update(combat_room_channel_id="100")
    clans["슈퍼맨"].update(combat_room_channel_id=100)   # 같은 연합 → 같은 전투방
    clans["썰자"].update(combat_room_channel_id=None)
    config["combat_support"]["tts_channel_ids"] = ["11", 22, None, ""]
    config["combat_support"]["default_combat_room_channel_id"] = 300
    s = cs.Settings(config, CLANS)
    assert s.alert_channel_id == 999
    assert s.clan_of({"슈퍼맨", "운영진"}) == ("슈퍼맨", "🦸")
    assert s.clan_of({"정도", "슈퍼맨"}) == ("정도", "⚔️")
    assert s.clan_of({"손님"}) is None
    assert s.combat_room_for("정도") == 100
    assert s.combat_room_for("썰자") == 300     # 전투방 미설정 → 기본 전투방
    assert s.combat_room_for("") == 300
    assert s.tts_channels() == [11, 22]          # 적힌 ID 개수만큼 (빈 칸 제외)
    config["combat_support"]["tts_channel_ids"] = [11]
    assert cs.Settings(config, CLANS).tts_channels() == [11]   # 1개면 1번
    config["combat_support"]["tts_channel_ids"] = 33            # 목록 대신 숫자 하나도 허용
    assert cs.Settings(config, CLANS).tts_channels() == [33]
    assert s.tts_text("축순 3번", "정도", "투신") == "전투지원 요청, 축순 3번"
    config["combat_support"]["tts_template"] = "{clan} {name} 전투지원, {content} {oops}"
    assert cs.Settings(config, CLANS).tts_text("축순 3번") == "전투지원 요청, 축순 3번"


def test_empty_ids_are_none():
    config = {}
    cs.ensure_section(config, CLANS)
    s = cs.Settings(config, CLANS)
    assert s.alert_channel_id is None
    assert s.tts_channels() == []
    assert s.combat_room_for("정도") is None


def test_clean_content_and_merge():
    assert cs.clean_content("  축순  3번\n지원  ") == "축순 3번 지원"
    assert cs.clean_content("가" * 30) == "가" * 15
    assert cs.merge_attendees(["1", "2"], add=["2", "3"]) == ["1", "2", "3"]
    assert cs.merge_attendees(["1", "2", "3"], remove=["2"]) == ["1", "3"]


def test_store_roundtrip_and_trim():
    with tempfile.TemporaryDirectory() as d:
        st = cs.Store(os.path.join(d, "combat_support.json"))
        st.put(1, {"content": "a", "created_at": "2026-11-05T10:00:00", "attendees": []})
        rec, changed = st.update_attendees(1, lambda cur: cs.merge_attendees(cur, add=["7", "8"]))
        assert changed and rec["attendees"] == ["7", "8"]
        rec, changed = st.update_attendees(1, lambda cur: cs.merge_attendees(cur, add=["7"]))
        assert not changed
        assert st.update_attendees(2, lambda cur: cur) == (None, False)
        for i in range(cs.KEEP_RECORDS + 5):
            st.put(1000 + i, {"created_at": f"2027-01-01T00:{i // 60:02d}:{i % 60:02d}", "attendees": []})
        data = st.load()
        assert len(data) == cs.KEEP_RECORDS
        assert "1" not in data   # 가장 오래된 것부터 정리


def test_embed_with_and_without_clans():
    rec = {"content": "축순 3번 기사단", "requester_name": "투신", "clan": "정도", "clan_emoji": "⚔️",
           "time": "14:05", "combat_room_id": 100, "attendees": ["1", "2", "3"]}
    names = {"1": "[정도]투신/기사/90", "2": "빼쥬", "3": "손님"}
    roles = {"1": {"정도"}, "2": {"슈퍼맨"}, "3": set()}

    def split(ids):
        return rbc.group_by_clan(ids, CLANS[:2], lambda u: roles[u], label_of=lambda u: names[u])

    e = cs.build_embed(rec, names, split)
    assert e.title == "⚔️ 전투지원 요청"
    assert e.description.startswith("📌 축순 3번 기사단\n🙋 투신 (⚔️ 정도) · 14:05  👉 <#100>")
    assert "✅ 현재 참석: 총 3명" in e.description
    fields = {f.name: f.value for f in e.fields}
    assert fields["⚔️ 정도 1명"] == "투신"
    assert fields["🦸 슈퍼맨 1명"] == "빼쥬"
    assert fields["❔ 미지정 1명"] == "손님"

    rec2 = dict(rec, clan="", clan_emoji="", combat_room_id=None, attendees=[])
    e2 = cs.build_embed(rec2, names, None)
    assert "👉" not in e2.description
    assert "🙋 투신 · 14:05" in e2.description
    assert "✅ 현재 참석: 0명 | 없음" in e2.description
    assert cs.push_text(rec) == "⚔️ 전투지원 요청: 축순 3번 기사단"

    ended = dict(rec, ended_at="2026-11-05T15:00:00", ended_time="15:00")
    e3 = cs.build_embed(ended, names, split)
    assert e3.title == "⚔️ 전투지원 요청 (종료)"
    assert "🔒 전투종료 15:00" in e3.description and "✅ 최종 참석: 총 3명" in e3.description
    assert e3.color.value == 0x888888
    assert cs.push_text(ended) == "⚔️ 전투지원 요청 (종료): 축순 3번 기사단"


def test_announce_sends_one_speak_per_clan_room():
    config = {}
    cs.ensure_section(config, CLANS)
    config["combat_support"]["tts_channel_ids"] = [11, 22]
    sent = []

    async def fake_ipc(port, cmd, args, **kw):
        sent.append((port, cmd, args))
        return {"ok": args[2] != 22}

    orig = cs.send_ipc
    cs.send_ipc = fake_ipc
    try:
        obj = cs.CombatSupport(None, config, CLANS, tempfile.gettempdir(), is_admin=lambda m: True,
                               clan_split_ids=lambda ids: None, id_to_name=dict,
                               update_user_mapping=lambda u, n: None, tts_port=7704)
        n = asyncio.run(obj.announce({"content": "축순 3번", "clan": "정도", "requester_name": "투신"}))
        assert n == 1
        assert sent == [(7704, "speak", ["전투지원 요청, 축순 3번", "2", 11]),
                        (7704, "speak", ["전투지원 요청, 축순 3번", "2", 22])]
        obj.tts_port = 0
        assert asyncio.run(obj.announce({"content": "x"})) == 0
    finally:
        cs.send_ipc = orig


def test_initial_channels_fill_only_empty():
    config = {}
    cs.ensure_section(config, CLANS)
    cs.apply_initial_channels(config)
    s = cs.Settings(config, CLANS)
    assert s.alert_channel_id == 1567766439123755028
    assert s.combat_room_for("정도") == 1567388527442919586
    assert len(s.tts_channels()) == 5
    config["combat_support"]["tts_channel_ids"] = [1]
    config[cs.ALERT_CHANNEL_KEY] = 2
    cs.apply_initial_channels(config)
    assert config["combat_support"]["tts_channel_ids"] == [1] and config[cs.ALERT_CHANNEL_KEY] == 2


def test_announce_prefers_music_bots_then_falls_back():
    config = {}
    cs.ensure_section(config, CLANS)
    config["combat_support"]["tts_channel_ids"] = [11, 22, 33]
    sent = []
    music_ok = {"v": True}

    async def fake_ipc(port, cmd, args, **kw):
        sent.append((port, cmd, args))
        if cmd == "announce":
            return {"ok": True, "channels": 3} if music_ok["v"] else {"ok": False, "error": "off"}
        return {"ok": True}

    orig = cs.send_ipc
    cs.send_ipc = fake_ipc
    try:
        obj = cs.CombatSupport(None, config, CLANS, tempfile.gettempdir(), is_admin=lambda m: True,
                               clan_split_ids=lambda ids: None, id_to_name=dict,
                               update_user_mapping=lambda u, n: None, tts_port=7704, music_port=7705)
        assert asyncio.run(obj.announce({"content": "축순 3번"})) == 3
        assert sent == [(7705, "announce", ["전투지원 요청, 축순 3번", "2", [11, 22, 33]])]
        sent.clear()
        music_ok["v"] = False
        assert asyncio.run(obj.announce({"content": "축순 3번"})) == 3
        assert [c for _, c, _ in sent] == ["announce", "speak", "speak", "speak"]
        sent.clear()
        config["combat_support"]["tts_engine"] = "tts"
        obj.reload()
        asyncio.run(obj.announce({"content": "x"}))
        assert [c for _, c, _ in sent] == ["speak", "speak", "speak"]
    finally:
        cs.send_ipc = orig



def test_view_buttons_and_end_permission():
    async def run():
        obj = cs.CombatSupport(None, {}, CLANS, tempfile.gettempdir(), is_admin=lambda m: m.id == 1,
                               clan_split_ids=lambda ids: None, id_to_name=dict,
                               update_user_mapping=lambda u, n: None)
        live = {c.custom_id: c.disabled for c in cs.CombatView(obj).children}
        assert live == {cs.BTN_VOICECHECK: False, cs.BTN_ADD: False, cs.BTN_REMOVE: False, cs.BTN_END: False}
        done = {c.custom_id: c.disabled for c in cs.CombatView(obj, ended=True).children}
        assert done == {cs.BTN_VOICECHECK: True, cs.BTN_ADD: False, cs.BTN_REMOVE: False, cs.BTN_END: True}

        class U:
            def __init__(self, i):
                self.id = i
        rec = {"requester_id": "5"}
        assert obj.can_end(U(1), rec) and obj.can_end(U(5), rec) and not obj.can_end(U(7), rec)
    asyncio.run(run())


def test_store_update():
    with tempfile.TemporaryDirectory() as d:
        st = cs.Store(os.path.join(d, "c.json"))
        st.put(1, {"created_at": "x", "attendees": []})
        rec = st.update(1, lambda r: r.setdefault("ended_at", "t"))
        assert rec["ended_at"] == "t" and st.get(1)["ended_at"] == "t"
        assert st.update(2, lambda r: None) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
