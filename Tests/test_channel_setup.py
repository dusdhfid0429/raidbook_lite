# -*- coding: utf-8 -*-
"""RaidBook_channel_setup 자동 생성 로직 테스트 (가짜 Guild 객체)."""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from RaidBook_channel_setup import ChannelSpec, ensure_channels  # noqa: E402


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.text_channels, self.voice_channels = [], []


class FakeGuild:
    def __init__(self, manage=True):
        self.id = 999
        self.categories = []
        self.channels = {}
        self.default_role = object()
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=manage))
        self._next = 100

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def create_category(self, name, **kw):
        c = FakeCategory(name)
        self.categories.append(c)
        return c

    async def _mk(self, name, voice, category, **kw):
        self._next += 1
        ch = SimpleNamespace(id=self._next, name=name, guild=self)
        self.channels[ch.id] = ch
        (category.voice_channels if voice else category.text_channels).append(ch)
        return ch

    async def create_text_channel(self, name, **kw):
        return await self._mk(name, False, **kw)

    async def create_voice_channel(self, name, **kw):
        return await self._mk(name, True, **kw)


def _run(guild, config):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    specs = [ChannelSpec("alert_channel_id", "보탐-알림"),
             ChannelSpec("voice_channel_ids", "보탐-음성", voice=True, is_list=True)]
    bot = SimpleNamespace(guilds=[guild], get_guild=lambda i: guild, get_channel=guild.get_channel)
    created, problems = asyncio.run(ensure_channels(bot, path, config, specs))
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    os.remove(path)
    return created, problems, saved


def test_creates_missing_and_saves():
    g = FakeGuild()
    created, problems, saved = _run(g, {"alert_channel_id": 0, "voice_channel_ids": [], "token": "t"})
    assert not problems
    assert set(created) == {"alert_channel_id", "voice_channel_ids"}
    assert isinstance(saved["alert_channel_id"], int) and saved["token"] == "t"
    assert isinstance(saved["voice_channel_ids"], list)


def test_existing_channel_kept():
    g = FakeGuild()
    g.channels[5] = SimpleNamespace(id=5, name="x", guild=g)
    g.channels[6] = SimpleNamespace(id=6, name="v", guild=g)
    created, problems, saved = _run(g, {"alert_channel_id": 5, "voice_channel_ids": [6]})
    assert created == {} and saved["alert_channel_id"] == 5


def test_no_permission_reports():
    created, problems, _ = _run(FakeGuild(manage=False), {"alert_channel_id": None})
    assert created == {} and problems


def test_convert_text_auction_to_voice():
    g = FakeGuild()
    cat = FakeCategory("보탐")
    old = SimpleNamespace(id=7, name="경매장", guild=g, category=cat, overwrites={}, position=3)
    g.channels[7] = old
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    config = {"auction_channel_id": 7, "token": "t"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    bot = SimpleNamespace(guilds=[g], get_guild=lambda i: g, get_channel=g.get_channel)
    replaced = {}
    specs = [ChannelSpec("auction_channel_id", "경매장", voice=True, convert=True)]
    created, problems = asyncio.run(ensure_channels(bot, path, config, specs, replaced=replaced))
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    os.remove(path)
    assert not problems and replaced == {"auction_channel_id": 7}
    new_id = created["auction_channel_id"][0]
    assert saved["auction_channel_id"] == new_id != 7 and saved["token"] == "t"
    assert [c.name for c in cat.voice_channels] == ["경매장"]     # 같은 이름·카테고리의 음성채널
    assert 7 in g.channels                                         # 예전 텍스트 채널은 남김
    assert not g.categories                                        # 변환만 할 땐 RaidBook 카테고리 안 만듦


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
