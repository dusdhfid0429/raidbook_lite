# -*- coding: utf-8 -*-
"""RaidBook_tts 테스트 — '#' 메시지 해석, 읽을 문장 정리."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_tts as rbt  # noqa: E402


def test_parse():
    assert rbt.parse_tts_message("#안녕하세요") == ("2", "안녕하세요")
    assert rbt.parse_tts_message("# 안녕", default_voice="1") == ("1", "안녕")
    assert rbt.parse_tts_message("#1 보스 떴어요") == ("1", "보스 떴어요")
    assert rbt.parse_tts_message("#2  집합") == ("2", "집합")
    assert rbt.parse_tts_message("#1") == (None, None)          # 목소리만 있고 내용 없음
    assert rbt.parse_tts_message("#") == (None, None)
    assert rbt.parse_tts_message("## 공지 제목") == (None, None)  # 디스코드 제목 서식
    assert rbt.parse_tts_message("안녕 #태그") == (None, None)
    assert rbt.parse_tts_message("#10시 집합") == ("2", "10시 집합")
    assert rbt.parse_tts_message("#안녕", default_voice="9") == ("2", "안녕")


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
