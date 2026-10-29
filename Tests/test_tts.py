# -*- coding: utf-8 -*-
"""RaidBook_tts 테스트 — /tts 결과 메시지 형식·되읽기, 읽을 문장 정리."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_tts as rbt  # noqa: E402


def test_post_roundtrip():
    post = rbt.format_post("1", "동훈", "보스 떴어요: 집합!")
    assert post == "🔊 👨 **동훈**: 보스 떴어요: 집합!", post
    assert rbt.parse_post(post) == ("1", "동훈", "보스 떴어요: 집합!")
    multi = rbt.format_post("2", "a**b", "첫 줄\n둘째 줄")
    assert rbt.parse_post(multi) == ("2", "a b", "첫 줄\n둘째 줄"), rbt.parse_post(multi)
    assert rbt.format_post("9", "x", "y").startswith("🔊 👩")   # 모르는 목소리 → 여성
    assert rbt.parse_post("그냥 채팅") is None
    assert rbt.parse_post("") is None


def test_voice_prefix():
    assert rbt.split_voice_prefix("1 보스 떴어요") == ("1", "보스 떴어요")
    assert rbt.split_voice_prefix("2  집합") == ("2", "집합")
    assert rbt.split_voice_prefix("1") == ("1", "")
    assert rbt.split_voice_prefix("10시 집합") == (None, "10시 집합")
    assert rbt.split_voice_prefix(" 안녕 ") == (None, "안녕")
    assert rbt.VOICES == {"1": "ko-KR-InJoonNeural", "2": "ko-KR-SunHiNeural"}   # 전체판과 같은 목소리


def test_clean():
    t = rbt.clean_for_speech("이거 봐 https://youtu.be/abc <:pepe:123> <@42> <@!43>", {42: "철수"})
    assert t == "이거 봐 링크 pepe 철수", t
    long = rbt.clean_for_speech("가" * 300, max_len=50)
    assert long.startswith("가" * 50) and long.endswith("이하 생략")
    assert rbt.clean_for_speech("   ") == ""


def test_speech_text():
    assert rbt.speech_text("동훈", "집합") == "동훈, 집합"
    assert rbt.speech_text("동훈", "집합", read_name=False) == "집합"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
