# -*- coding: utf-8 -*-
"""📢 /전파 — 운영진이 정해진 채널에서 쓴 글을 혈맹 대화방 전체에 음성으로 읽어 준다.

음성은 /전투지원과 같은 길(음악봇들이 대화방마다 하나씩 동시에, 꺼져 있으면 TTS 봇)로 나가고,
읽을 대화방 목록도 전투지원의 combat_support.tts_channel_ids 를 그대로 쓴다.
설정은 boss_config.json 의 broadcast:
  enabled         켜기/끄기
  channel_ids     /전파를 쓸 수 있는 채널 ID 목록 (비면 모든 채널)
  max_length      글자 수 제한 (넘으면 앞부분만)
  tts_template    읽을 문장 ({name}=닉네임, {content}=내용)
"""
import RaidBook_clans as rb_clans

DEFAULT_MAX_LENGTH = 50
DEFAULT_TEMPLATE = "{name}님 전달. {content}"
# 동훈님이 정한 전파 채널 (2026-11-05)
INITIAL_CHANNEL = 1567863699484577942

SECTION_COMMENT = (
    "/전파 설정. channel_ids = /전파를 쓸 수 있는 채널 ID 목록(비면 모든 채널), "
    "누가 쓸 수 있는지는 권한 표(RaidBook_perms, 운영진)에서 정해요. "
    "max_length = 글자 수 제한, tts_template = 읽을 문장({name}=닉네임, {content}=내용). "
    "읽어 줄 대화방은 combat_support.tts_channel_ids 를 같이 써요. 고친 뒤 보스봇을 다시 시작하세요."
)


def _cid(value):
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def default_section() -> dict:
    return {
        "enabled": True,
        "channel_ids": [INITIAL_CHANNEL],
        "max_length": DEFAULT_MAX_LENGTH,
        "tts_template": DEFAULT_TEMPLATE,
    }


def ensure_section(config) -> bool:
    """broadcast 자리가 없거나 빠진 칸이 있으면 채운다. 바뀌었으면 True (운영진이 고친 값은 그대로)."""
    changed = False
    sec = config.get("broadcast")
    if not isinstance(sec, dict):
        config["broadcast"] = default_section()
        changed = True
    else:
        # 예전 role_suffix('운영진'으로 끝나는 역할) 칸은 없앤다 — 권한은 RaidBook_perms 권한 표 (2026-11-06)
        if "role_suffix" in sec:
            sec.pop("role_suffix")
            changed = True
        for k, v in default_section().items():
            if k not in sec:
                sec[k] = v
                changed = True
    if config.get("_broadcast_comment") != SECTION_COMMENT:
        config["_broadcast_comment"] = SECTION_COMMENT
        changed = True
    return changed


class Settings:
    def __init__(self, config):
        sec = config.get("broadcast") if isinstance(config.get("broadcast"), dict) else {}
        self.enabled = bool(sec.get("enabled", True))
        raw = sec.get("channel_ids")
        raw = raw if isinstance(raw, list) else [raw]
        self.channel_ids = [c for c in (_cid(v) for v in raw) if c]
        try:
            self.max_length = max(1, min(200, int(sec.get("max_length") or DEFAULT_MAX_LENGTH)))
        except (TypeError, ValueError):
            self.max_length = DEFAULT_MAX_LENGTH
        self.template = str(sec.get("tts_template") or DEFAULT_TEMPLATE)

    def channel_allowed(self, channel_id) -> bool:
        return not self.channel_ids or _cid(channel_id) in self.channel_ids

    def clean(self, text) -> str:
        return " ".join(str(text or "").split())[:self.max_length]

    def speech(self, content, name) -> str:
        try:
            text = self.template.format(content=content, name=name or "")
        except (KeyError, IndexError, ValueError):
            text = DEFAULT_TEMPLATE.format(content=content, name=name or "")
        return " ".join(text.split())


def spoken_name(display_name, clan_names=()) -> str:
    """'[정도]투신/기사/90' → '투신' (음성은 짧게)."""
    return rb_clans.short_name(display_name, clan_names)


def post_text(display_name, content) -> str:
    return f"📢 **{display_name}**: {content}"


def result_line(spoken, total) -> str:
    if total <= 0:
        return "-# ⚠️ 읽어 줄 대화방이 설정되지 않았어요 (combat_support.tts_channel_ids)"
    if spoken <= 0:
        return "-# ⚠️ 대화방 음성 전달 실패 (음악봇·TTS 봇이 꺼져 있는지 확인)"
    return f"-# 🔊 대화방 {spoken}/{total}곳에 전달했어요"
