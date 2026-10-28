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


def test_ytdlp_args_with_deno():
    assert rbm.ytdlp_base_args("C:/x/deno.exe")[-2:] == ["--js-runtimes", "deno:C:/x/deno.exe"]
    assert "--js-runtimes" not in rbm.ytdlp_base_args("")


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
