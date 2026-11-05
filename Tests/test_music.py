# -*- coding: utf-8 -*-
"""RaidBook_music 테스트 — 대기열, 링크 판정, yt-dlp 결과 해석(가짜 yt-dlp), 슬래시 명령 공존."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_music as rbm  # noqa: E402

# 인자를 보고 yt-dlp처럼 응답하는 가짜 실행 파일
FAKE_YTDLP = r'''
import json, sys
args = sys.argv[1:]
target = args[-1]
if "bad" in target:
    print("ERROR: [youtube] bad: Video unavailable", file=sys.stderr); sys.exit(1)
if "-g" in args:
    print("https://rr1.googlevideo.com/audio?id=" + target[-3:]); sys.exit(0)
if target.startswith("ytsearch1:"):
    print(json.dumps({"_type": "playlist", "entries": [{"id": "abc", "title": "검색결과", "duration": 61, "url": "abc"}]}))
elif "list=" in target:
    print(json.dumps({"_type": "playlist", "entries": [
        {"id": "v%d" % i, "title": "곡%d" % i, "duration": 100 + i, "url": "https://www.youtube.com/watch?v=v%d" % i}
        for i in range(5)]}))
else:
    print(json.dumps({"id": "one", "title": "단일 영상", "duration": 3725, "webpage_url": target}))
'''


def _fake_cmd(tmp):
    p = os.path.join(tmp, "fake_ytdlp.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(FAKE_YTDLP)
    return [sys.executable, p]


def test_url_checks():
    assert rbm.is_youtube_url("https://www.youtube.com/watch?v=x")
    assert rbm.is_youtube_url("https://youtu.be/x")
    assert rbm.is_youtube_url("https://music.youtube.com/watch?v=x")
    assert not rbm.is_youtube_url("https://evil.com/youtube.com/x")
    assert not rbm.is_url("아이유 좋은날")


def test_duration():
    assert rbm.format_duration(61) == "1:01"
    assert rbm.format_duration(3725) == "1:02:05"
    assert rbm.format_duration(None) == "LIVE"


def test_queue_limit_and_remove():
    q = rbm.MusicQueue(max_size=3)
    assert q.add([rbm.Track(f"t{i}", f"u{i}") for i in range(5)]) == 3
    assert q.remove(2).title == "t1" and len(q) == 2
    assert q.remove(9) is None
    assert q.pop().title == "t0"


def test_lookup_single_playlist_search():
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _fake_cmd(tmp)
        one = asyncio.run(rbm.lookup(cmd, "https://www.youtube.com/watch?v=one", requester_name="동훈"))
        assert len(one) == 1 and one[0].title == "단일 영상" and one[0].duration_text == "1:02:05"
        assert one[0].requester_name == "동훈"
        pl = asyncio.run(rbm.lookup(cmd, "https://www.youtube.com/playlist?list=PL1", limit=3))
        assert [t.title for t in pl] == ["곡0", "곡1", "곡2"]
        s = asyncio.run(rbm.lookup(cmd, "아이유 좋은날"))
        assert s[0].url == "https://www.youtube.com/watch?v=abc"
        url = asyncio.run(rbm.stream_url(cmd, "https://www.youtube.com/watch?v=one"))
        assert url.startswith("https://rr1.googlevideo.com/")
        try:
            asyncio.run(rbm.lookup(cmd, "https://www.youtube.com/watch?v=bad"))
            raise AssertionError("실패해야 함")
        except RuntimeError as e:
            assert "Video unavailable" in str(e)


def test_voice_bitrate():
    assert rbm.voice_bitrate_kbps(64000) == 128
    assert rbm.voice_bitrate_kbps(384000) == 384
    assert rbm.voice_bitrate_kbps(96000, 256) == 256


def test_pipe_args_stream_to_stdout():
    args = rbm.ytdlp_pipe_args("https://youtu.be/x", "")
    assert args[-3:] == ["-o", "-", "https://youtu.be/x"] and "--no-playlist" in args


def test_ytdlp_args_with_deno():
    assert rbm.ytdlp_base_args("C:/x/deno.exe")[-2:] == ["--js-runtimes", "deno:C:/x/deno.exe"]
    assert "--js-runtimes" not in rbm.ytdlp_base_args("")


def test_pick_bot_keeps_channels_apart():
    # 봇 A는 채널 1에서 재생 중, B는 채널 2에서 재생 중, C·D는 비어 있음
    bots = [("A", 1, True), ("B", 2, True), ("C", None, False), ("D", None, False)]
    assert rbm.pick_bot(bots, 1) == "A"          # 같은 채널은 같은 봇 (대기열 이어 붙임)
    assert rbm.pick_bot(bots, 2) == "B"
    assert rbm.pick_bot(bots, 3) == "C"          # 새 채널은 빈 봇
    full = [("A", 1, True), ("B", 2, True), ("C", 3, True), ("D", 4, True)]
    assert rbm.pick_bot(full, 5) is None          # 모두 바쁘면 다른 채널 것을 뺏지 않음
    idle = [("A", 1, True), ("B", 2, False)]
    assert rbm.pick_bot(idle, 9) == "B"           # 재생이 끝나 놀고 있는 봇은 옮겨 씀


def test_queues_are_separate_per_bot():
    a, b = rbm.MusicQueue(max_size=10), rbm.MusicQueue(max_size=10)
    a.add([rbm.Track("a1", "u"), rbm.Track("a2", "u")])
    b.add([rbm.Track("b1", "u")])
    assert [t.title for t in a.items] == ["a1", "a2"] and [t.title for t in b.items] == ["b1"]


class FakeTree:
    """sync는 목록 전체를 덮어쓰고, upsert는 하나만 바꾸는 Discord 동작 흉내."""

    def __init__(self, server, own):
        self.server, self.own = server, own
        self.client = SimpleNamespace(application_id=1, http=SimpleNamespace(upsert_guild_command=self._upsert))

    async def fetch_commands(self, guild=None):
        return [SimpleNamespace(name=n, to_dict=lambda n=n: {"id": 5, "type": 1, "name": n, "description": "d",
                                                             "options": [], "contexts": None})
                for n in self.server]

    async def sync(self, guild=None):
        self.server[:] = list(self.own)
        return [SimpleNamespace(name=n) for n in self.own]

    async def _upsert(self, app_id, guild_id, payload):
        assert "id" not in payload and "contexts" not in payload
        if payload["name"] not in self.server:
            self.server.append(payload["name"])


def test_boss_sync_keeps_music_commands():
    server = ["재생", "정지", "인원체크", "옛날명령"]
    tree = FakeTree(server, own=["인원체크", "경매"])
    asyncio.run(rbm.sync_guild_keeping(tree, SimpleNamespace(id=9)))
    assert sorted(server) == sorted(["인원체크", "경매", "재생", "정지"])   # 음악 명령 유지, 지운 보스 명령은 삭제


def test_assign_announce_one_bot_per_room():
    bots = [("A", None, False), ("B", 5, True), ("C", 3, False), ("D", None, False), ("E", 9, False)]
    # 이미 그 방에 있는 봇(C→3, B→5, 재생 중이어도) → 음성에 없는 봇 → 놀고 있는 봇
    plan = rbm.assign_announce(bots, [1, 2, 3, 4, 5])
    assert sorted((b, cs) for b, cs in plan) == [("A", [1]), ("B", [5]), ("C", [3]), ("D", [2]), ("E", [4])]
    # 놀고 있는 봇이 없으면 음악 재생 중인 봇도 쓴다 (재생 종료 후 안내)
    plan = rbm.assign_announce([("A", 7, True), ("B", 8, True)], [1, 2])
    assert sorted(plan) == [("A", [1]), ("B", [2])]
    # 봇보다 방이 많으면 배정된 봇이 이어서 읽는다
    plan = rbm.assign_announce([("A", None, False), ("B", None, False)], [1, 2, 3, 4, 5])
    assert dict(plan) == {"A": [1, 3, 5], "B": [2, 4]}
    # 권한 없는 방에는 배정하지 않음
    plan = rbm.assign_announce([("A", None, False), ("B", None, False)], [1, 2], can_use=lambda b, c: b == "A")
    assert dict(plan) == {"A": [1, 2]}
    assert rbm.assign_announce([], [1]) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
