# -*- coding: utf-8 -*-
"""
필요한 Discord 채널 자동 생성.

config에 채널 ID가 비어 있거나 가리키는 채널이 서버에서 사라졌으면
"RaidBook" 카테고리 아래에 새로 만들고 ID를 config 파일에 저장한다.
봇에 '채널 관리' 권한이 없으면 만들지 않고 이유만 돌려준다.
"""
import json
from dataclasses import dataclass

import discord

from RaidBook_common import write_json_atomic

CATEGORY_NAME = "RaidBook"


@dataclass
class ChannelSpec:
    key: str             # config 키 (예: alert_channel_id)
    name: str            # 새로 만들 채널 이름
    voice: bool = False  # 음성채널 여부
    is_list: bool = False  # config 값이 ID 목록인지 (voice_channel_ids)
    private: bool = False  # @everyone 에게 숨김 (관리 채널)


def _ids(value):
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value if str(v).strip().isdigit()]
    if value and str(value).strip().isdigit():
        return [int(value)]
    return []


def resolve_guild(bot, config, keys):
    """config guild_id → 설정된 채널이 속한 서버 → 봇이 한 서버에만 있으면 그 서버."""
    gid = config.get("guild_id")
    if gid and str(gid).isdigit():
        guild = bot.get_guild(int(gid))
        if guild:
            return guild
    for key in keys:
        for cid in _ids(config.get(key)):
            ch = bot.get_channel(cid)
            if ch is not None and getattr(ch, "guild", None):
                return ch.guild
    if len(bot.guilds) == 1:
        return bot.guilds[0]
    return None


async def ensure_channels(bot, config_path, config, specs, logger=None, *, guild=None):
    """없는 채널을 만들고 config(메모리+파일)를 갱신한다.

    Returns: (created: {key: [채널 ID...]}, problems: [str])
    """
    created, problems = {}, []
    guild = guild or resolve_guild(bot, config, [s.key for s in specs])
    if guild is None:
        problems.append("서버를 정할 수 없어 채널 자동 생성을 건너뜀 (config에 guild_id를 넣어 주세요)")
        return created, problems

    missing = []
    for spec in specs:
        ids = _ids(config.get(spec.key))
        alive = [cid for cid in ids if guild.get_channel(cid) is not None]
        if not alive:
            missing.append(spec)
    if not missing:
        return created, problems

    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        names = ", ".join(f"#{s.name}" for s in missing)
        problems.append(f"'채널 관리' 권한이 없어 {names} 채널을 만들지 못함")
        return created, problems

    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    try:
        if category is None:
            category = await guild.create_category(CATEGORY_NAME, reason="RaidBook 채널 자동 생성")
        for spec in missing:
            # 같은 이름 채널이 카테고리에 이미 있으면 재사용 (재시작 반복 생성 방지)
            pool = category.voice_channels if spec.voice else category.text_channels
            ch = discord.utils.get(pool, name=spec.name)
            if ch is None:
                overwrites = None
                if spec.private:
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        embed_links=True, read_message_history=True),
                    }
                kwargs = {"category": category, "reason": "RaidBook 채널 자동 생성"}
                if overwrites:
                    kwargs["overwrites"] = overwrites
                if spec.voice:
                    ch = await guild.create_voice_channel(spec.name, **kwargs)
                else:
                    ch = await guild.create_text_channel(spec.name, **kwargs)
            config[spec.key] = [ch.id] if spec.is_list else ch.id
            created[spec.key] = [ch.id]
            if logger:
                logger.info(f"[채널 자동 생성] {spec.key} → #{ch.name} ({ch.id})")
    except discord.Forbidden:
        problems.append("Discord가 채널 생성을 거부함 (봇 역할 권한/순서 확인)")
    except discord.HTTPException as e:
        problems.append(f"채널 생성 실패: {e}")

    if created:
        # 파일의 최신 내용 위에 바뀐 키만 덮어쓴다 (GUI 등에서 동시에 고친 값 보존)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
        except Exception:
            on_disk = dict(config)
        if "guild_id" in on_disk or "guild_id" in config:
            on_disk["guild_id"] = guild.id
            config["guild_id"] = guild.id
        for key in created:
            on_disk[key] = config[key]
        write_json_atomic(config_path, on_disk)
    return created, problems
