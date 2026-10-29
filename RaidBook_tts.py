# -*- coding: utf-8 -*-
"""TTS 봇 공용 로직 — `#` 메시지 해석, 읽을 문장 정리 (Discord 의존 없음, 테스트 가능).

#텍스트      → 기본 목소리
#1 텍스트    → 남성 (ko-KR-InJoonNeural)
#2 텍스트    → 여성 (ko-KR-SunHiNeural)
"""
import re

VOICES = {
    "1": "ko-KR-InJoonNeural",
    "2": "ko-KR-SunHiNeural",
}
VOICE_LABELS = {"1": "👨 남성", "2": "👩 여성"}

TTS_COMMAND_NAMES = ("tts퇴장", "tts건너뛰기", "tts대기열")

_URL = re.compile(r"https?://\S+")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_MENTION = re.compile(r"<(@!?|@&|#)(\d+)>")
_SPACES = re.compile(r"\s+")


def voice_key(key) -> str:
    """config 값(1/2, 숫자)을 VOICES 키로. 모르는 값이면 여성(2)."""
    k = str(key or "").strip()
    return k if k in VOICES else "2"


def parse_tts_message(content: str, default_voice="2"):
    """'#'으로 시작하는 메시지 → (목소리 키, 텍스트). 대상이 아니면 (None, None).

    '## 제목'처럼 #이 여러 개면 디스코드 제목 서식이므로 읽지 않는다.
    """
    content = (content or "").strip()
    if not content.startswith("#") or content.startswith("##"):
        return None, None
    raw = content[1:].strip()
    if not raw:
        return None, None
    parts = raw.split(None, 1)
    if parts[0] in VOICES:
        if len(parts) < 2 or not parts[1].strip():
            return None, None
        return parts[0], parts[1].strip()
    return voice_key(default_voice), raw


def clean_for_speech(text: str, names=None, max_len: int = 200) -> str:
    """읽기 좋게 정리 — 링크는 '링크', 커스텀 이모지는 이름, 멘션은 이름(names: {id: 이름})."""
    names = names or {}
    text = _URL.sub(" 링크 ", text or "")
    text = _CUSTOM_EMOJI.sub(lambda m: f" {m.group(1)} ", text)
    text = _MENTION.sub(lambda m: f" {names.get(int(m.group(2)), '')} ", text)
    text = _SPACES.sub(" ", text).strip()
    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip() + " 이하 생략"
    return text


def speech_text(display_name: str, text: str, read_name: bool = True) -> str:
    return f"{display_name}, {text}" if read_name and display_name else text
