# -*- coding: utf-8 -*-
"""TTS 봇 공용 로직 — /tts 결과 메시지 형식, 읽을 문장 정리 (Discord 의존 없음, 테스트 가능).

/tts 내용:<텍스트> [목소리:남성|여성] → 채널에 '🔊 👩 **닉네임**: 텍스트' 메시지 + 🔄 다시 듣기 버튼.
다시 듣기는 이 메시지 내용을 해석해서 읽는다 (재시작 후에도 동작).
"""
import re

VOICES = {
    "1": "ko-KR-InJoonNeural",
    "2": "ko-KR-SunHiNeural",
}
VOICE_LABELS = {"1": "👨 남성", "2": "👩 여성"}
VOICE_ICONS = {"1": "👨", "2": "👩"}
_ICON_VOICE = {v: k for k, v in VOICE_ICONS.items()}

TTS_COMMAND_NAMES = ("tts", "tts나가기", "tts스킵", "tts대기열")

_URL = re.compile(r"https?://\S+")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_MENTION = re.compile(r"<(@!?|@&|#)(\d+)>")
_SPACES = re.compile(r"\s+")
_MD = re.compile(r"\*+|\n")
_POST = re.compile(r"^🔊 (👨|👩) \*\*(.*?)\*\*: (.+)$", re.S)


def voice_key(key) -> str:
    """config 값(1/2, 숫자)을 VOICES 키로. 모르는 값이면 여성(2)."""
    k = str(key or "").strip()
    return k if k in VOICES else "2"


def split_voice_prefix(text: str):
    """전체판 '#1 내용'/'#2 내용' 습관 그대로 — '1 내용' → ("1", "내용"). 접두어가 없으면 (None, 원문)."""
    text = (text or "").strip()
    parts = text.split(None, 1)
    if parts and parts[0] in VOICES:
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")
    return None, text


def format_post(voice: str, display_name: str, text: str) -> str:
    """채널에 남기는 메시지 — 다시 듣기 버튼이 이 내용을 parse_post로 되읽는다."""
    return f"🔊 {VOICE_ICONS[voice_key(voice)]} **{_MD.sub(' ', display_name)}**: {text}"


def parse_post(content: str):
    """format_post 메시지 → (목소리 키, 닉네임, 텍스트). 형식이 다르면 None."""
    m = _POST.match(content or "")
    if not m:
        return None
    return _ICON_VOICE[m.group(1)], m.group(2), m.group(3)


def clean_for_speech(text: str, names=None, max_len: int = 0) -> str:
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


_COUNT_WORDS = {1: "하나", 2: "둘", 3: "셋", 4: "넷", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열"}


def count_word(n: int) -> str:
    """경매 마감 카운트 읽기 — 10 이하는 '다섯·넷…', 그 위는 숫자 그대로."""
    return _COUNT_WORDS.get(int(n), str(int(n)))


def countdown_plan(seconds: float, slack: float = 0.5):
    """카운트 시작 시점 기준 [(몇 초 뒤, 숫자)] — 마지막 숫자(1)는 마감 1초 전.
    시작이 늦어 남은 시간이 짧으면 그만큼 작은 숫자부터 읽는다."""
    import math
    seconds = float(seconds)
    top = max(1, int(math.floor(seconds + slack)))
    return [(max(0.0, seconds - n), n) for n in range(top, 0, -1)]
