# -*- coding: utf-8 -*-
"""RaidBook_broadcast (/전파) 테스트 — 설정 자리, 채널·역할 제한, 문장, 대화방 음성 전달."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_broadcast as bc  # noqa: E402
import RaidBook_combat_support as cs  # noqa: E402

CLANS = [("정도", "⚔️"), ("슈퍼맨", "🦸")]


def test_ensure_section_once_and_keeps_edits():
    config = {}
    assert bc.ensure_section(config) is True
    assert config["broadcast"]["channel_ids"] == [1567863699484577942]
    assert config["broadcast"]["max_length"] == 50
    assert bc.ensure_section(config) is False
    config["broadcast"]["channel_ids"] = [5]
    del config["broadcast"]["max_length"]
    assert bc.ensure_section(config) is True
    assert config["broadcast"]["channel_ids"] == [5] and config["broadcast"]["max_length"] == 50


def test_channel_and_role_limits():
    config = {}
    bc.ensure_section(config)
    s = bc.Settings(config)
    assert s.channel_allowed(1567863699484577942) and s.channel_allowed("1567863699484577942")
    assert not s.channel_allowed(123)
    assert s.can_use(["@everyone", "슈퍼맨운영진"])
    assert s.can_use(["정도운영진 "]) and s.can_use(["운영진"])
    assert not s.can_use(["슈퍼맨", "운영진님"]) and not s.can_use([])
    config["broadcast"]["channel_ids"] = []
    assert bc.Settings(config).channel_allowed(123)          # 비면 모든 채널
    config["broadcast"]["role_suffix"] = ""
    assert not bc.Settings(config).can_use(["운영진"])        # 비면 아무도 못 씀


def test_text_and_speech():
    config = {}
    bc.ensure_section(config)
    s = bc.Settings(config)
    long = "가" * 80
    assert len(s.clean(long)) == 50
    assert s.clean("  10시   기란\n집결 ") == "10시 기란 집결"
    name = bc.spoken_name("[정도]투신/기사/90", ["정도", "슈퍼맨"])
    assert name == "투신"
    assert s.speech("10시 기란 집결", name) == "투신님 전달. 10시 기란 집결"
    config["broadcast"]["tts_template"] = "{oops}"
    assert bc.Settings(config).speech("x", "a") == "a님 전달. x"
    assert bc.post_text("[정도]투신", "집결") == "📢 **[정도]투신**: 집결"
    assert bc.result_line(5, 5) == "-# 🔊 대화방 5/5곳에 전달했어요"
    assert "실패" in bc.result_line(0, 5) and "설정" in bc.result_line(0, 0)


def test_speak_uses_combat_rooms_via_music_bots():
    config = {}
    cs.ensure_section(config, CLANS)
    config["combat_support"]["tts_channel_ids"] = [11, 22]
    sent = []

    async def fake_ipc(port, cmd, args, **kw):
        sent.append((port, cmd, args))
        return {"ok": True, "channels": 2}

    orig = cs.send_ipc
    cs.send_ipc = fake_ipc
    try:
        obj = cs.CombatSupport(None, config, CLANS, tempfile.gettempdir(), is_admin=lambda m: True,
                               clan_split_ids=lambda ids: None, id_to_name=dict,
                               update_user_mapping=lambda u, n: None, tts_port=7704, music_port=7705)
        assert asyncio.run(obj.speak("투신님 전달. 집결", tag="전파")) == 2
        assert sent == [(7705, "announce", ["투신님 전달. 집결", "2", [11, 22]])]
        assert asyncio.run(obj.speak("", tag="전파")) == 0
    finally:
        cs.send_ipc = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
