# -*- coding: utf-8 -*-
"""🔐 /인증권한 — 서버의 모든 카테고리·채널을 '인증됨' 역할만 보이게 한 번에 바꾼다.

규칙 (채널마다):
  - @everyone       : 보기 끔 (다른 권한 칸은 그대로)
  - 인증됨          : 보기 켬 — 단, 원래 @everyone 보기가 꺼져 있던 비공개 채널은 그대로 숨김
  - 다른 역할 권한  : 지움. 이름이 verify.keep_role_suffix(기본 '운영진')로 끝나는 역할과
                      봇·연동 역할(managed)은 남김
  - 멤버 개별 권한  : 그대로
  - #인증 채널      : @everyone 보기 켬·글쓰기 끔, 인증됨은 숨김
바꾸기 전 모든 채널 권한을 Data/verify_perm_backup_*.json 에 남기고, `/인증권한 복구`로 되돌린다.
"""
import glob
import json
import os
from datetime import datetime

import discord

from RaidBook_common import write_json_atomic

BACKUP_PREFIX = "verify_perm_backup_"


def keep_role(role, keep_suffix) -> bool:
    if getattr(role, "managed", False):
        return True
    return bool(keep_suffix) and str(role.name).strip().endswith(keep_suffix)


def _copy(ow):
    if ow is None:
        return discord.PermissionOverwrite()
    allow, deny = ow.pair()
    return discord.PermissionOverwrite.from_pair(allow, deny)


def plan_channel(channel, everyone, verified, keep_suffix, verify_channel_id):
    """채널 하나의 새 권한 계산.

    Returns: (new_overwrites dict, removed_role_names list, private bool)
    """
    old = dict(channel.overwrites)
    cur_everyone = old.get(everyone)
    private = cur_everyone is not None and cur_everyone.view_channel is False
    new, removed = {}, []
    for target, perm in old.items():
        if target == everyone or target == verified:
            continue
        if isinstance(target, discord.Role) and not keep_role(target, keep_suffix):
            removed.append(target.name)
            continue
        new[target] = perm
    if verify_channel_id and channel.id == verify_channel_id:
        e = _copy(cur_everyone)
        e.view_channel, e.send_messages, e.read_message_history = True, False, True
        new[everyone] = e
        v = _copy(old.get(verified))
        v.view_channel = False
        new[verified] = v
        return new, removed, private
    e = _copy(cur_everyone)
    e.view_channel = False
    new[everyone] = e
    v = _copy(old.get(verified))
    if not private:
        v.view_channel = True
    if not v.is_empty():
        new[verified] = v
    return new, removed, private


def all_channels(guild):
    """카테고리 먼저, 그다음 채널 (카테고리 동기화 채널도 각각 직접 맞춤)."""
    return list(guild.categories) + [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]


def _same(a, b) -> bool:
    if set(a) != set(b):
        return False
    return all(a[k].pair() == b[k].pair() for k in a)


def build_plan(guild, verified, keep_suffix, verify_channel_id):
    """[(channel, new_overwrites, removed, private)] — 바뀌는 채널만."""
    out = []
    for ch in all_channels(guild):
        new, removed, private = plan_channel(ch, guild.default_role, verified, keep_suffix, verify_channel_id)
        if not _same(new, dict(ch.overwrites)):
            out.append((ch, new, removed, private))
    return out


def _label(ch):
    if isinstance(ch, discord.CategoryChannel):
        return f"📁 {ch.name}"
    return f"<#{ch.id}>"


def preview_text(plan, verified, keep_suffix, verify_channel_id) -> str:
    cats = sum(1 for ch, *_ in plan if isinstance(ch, discord.CategoryChannel))
    chans = len(plan) - cats
    role_counts = {}
    for _, _, removed, _ in plan:
        for name in removed:
            role_counts[name] = role_counts.get(name, 0) + 1
    private = [_label(ch) for ch, _, _, p in plan if p]
    lines = [f"🔐 **인증 권한 적용 미리보기** — 카테고리 {cats}개 · 채널 {chans}개를 바꿔요",
             f"• @everyone 보기 끔, **{verified.name}** 보기 켬"]
    if verify_channel_id:
        lines.append(f"• <#{verify_channel_id}>: @everyone만 보임(글쓰기 끔), {verified.name}은 숨김")
    if role_counts:
        rc = ", ".join(f"{n}({c})" for n, c in sorted(role_counts.items(), key=lambda x: -x[1]))
        lines.append(f"• 지울 역할 권한 (채널 수): {rc[:900]}")
    else:
        lines.append("• 지울 역할 권한 없음")
    keep = f"'{keep_suffix}'로 끝나는 역할, " if keep_suffix else ""
    lines.append(f"• 유지: {keep}봇·연동 역할, 멤버 개별 권한")
    if private:
        lines.append(f"• 비공개 채널은 {verified.name}에게 열지 않아요: {' '.join(private)[:700]}")
    lines.append("바꾸기 전 권한은 백업해 두고 `/인증권한 복구`로 되돌릴 수 있어요. 적용할까요?")
    return "\n".join(lines)


def _ser(overwrites):
    out = []
    for target, perm in overwrites.items():
        allow, deny = perm.pair()
        out.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": allow.value, "deny": deny.value})
    return out


def save_backup(data_dir, guild, logger=None) -> str:
    data = {"guild_id": guild.id, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channels": {str(ch.id): {"name": ch.name, "overwrites": _ser(ch.overwrites)}
                         for ch in all_channels(guild)}}
    path = os.path.join(data_dir, f"{BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(data_dir, exist_ok=True)
    write_json_atomic(path, data, logger)
    return path


def latest_backup(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, f"{BACKUP_PREFIX}*.json")))
    return files[-1] if files else None


def load_backup_overwrites(guild, entry):
    out = {}
    for o in entry.get("overwrites") or []:
        target = guild.get_role(o["id"]) if o.get("type") == "role" else guild.get_member(o["id"])
        if target is None:
            if o.get("type") == "role":
                continue   # 지워진 역할은 되살릴 수 없음
            target = discord.Object(id=o["id"], type=discord.Member)   # 서버를 나간 멤버
        out[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(o["allow"]),
                                                            discord.Permissions(o["deny"]))
    return out


async def apply_plan(plan, reason="RaidBook 서버 입장 인증 권한 적용"):
    ok, fails = 0, []
    for ch, new, _, _ in plan:
        try:
            await ch.edit(overwrites=new, reason=reason)
            ok += 1
        except Exception as e:
            fails.append(f"{_label(ch)} ({type(e).__name__})")
    return ok, fails


async def restore(guild, path, reason="RaidBook 인증 권한 복구"):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ok, fails = 0, []
    for cid, entry in (data.get("channels") or {}).items():
        ch = guild.get_channel(int(cid))
        if ch is None:
            continue
        try:
            await ch.edit(overwrites=load_backup_overwrites(guild, entry), reason=reason)
            ok += 1
        except Exception as e:
            fails.append(f"{_label(ch)} ({type(e).__name__})")
    return ok, fails


class ConfirmView(discord.ui.View):
    """미리보기 메시지의 ✅ 적용 / ❌ 취소 — 명령을 쓴 사람만."""

    def __init__(self, author_id, on_apply):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.on_apply = on_apply

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("명령을 쓴 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="적용", emoji="✅", style=discord.ButtonStyle.danger)
    async def apply(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.on_apply(interaction)

    @discord.ui.button(label="취소", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=interaction.message.content + "\n❌ 취소했어요.", view=self)
        self.stop()
