# -*- coding: utf-8 -*-
"""⚔️ 전투지원 요청 — /전투지원 → 내용 팝업(15자) → 전투알림 채널에 등록 + 혈맹 대화방 TTS 안내.

등록된 알림에는 요청자 혈맹(연합)의 전투방 바로가기 링크, 참석자(혈맹별), 인원체크·➕ 인원 추가·➖ 인원 제거·⏹️ 전투종료 버튼.
채널 ID는 전부 boss_config.json에서 고친다:
  combat_alert_channel_id               전투알림 채널 (비면 'RaidBook' 카테고리에 '전투-알림' 자동 생성)
  combat_support.tts_channel_ids                       TTS로 요청을 읽어 줄 대화방(음성채널) ID 목록 — 적힌 개수만큼 차례로 읽음
  combat_support.clans.<혈맹>.combat_room_channel_id    그 혈맹이 속한 연합 카테고리의 전투방(음성채널)
상태는 Data/combat_support.json (재시작 후에도 버튼 동작).
"""
import asyncio
import json
import os
import threading
from collections import defaultdict
from datetime import datetime

import discord

import RaidBook_perms as rb_perms
from discord import ui

import RaidBook_clans as rb_clans
from RaidBook_common import write_json_atomic
from RaidBook_ipc import send_ipc

ALERT_CHANNEL_KEY = "combat_alert_channel_id"
ALERT_CHANNEL_NAME = "전투-알림"
DEFAULT_MAX_LENGTH = 15
DEFAULT_PLACEHOLDER = "축순번호 등 간략하게 적어 주세요"
KEEP_RECORDS = 300          # 저장 파일에 남길 최근 알림 수
PICK_PAGE = 25              # 디스코드 선택 메뉴 최대 항목 수

BTN_VOICECHECK = "combat_voicecheck"
BTN_ADD = "combat_attendee_add"
BTN_REMOVE = "combat_attendee_remove"
BTN_END = "combat_end"


# ── 설정 (순수 로직) ─────────────────────────────────────────────────────
def _cid(value):
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def default_section(clan_roles) -> dict:
    """설정 파일에 처음 넣을 combat_support 자리 — 채널 ID는 비워 둔다."""
    return {
        "enabled": True,
        "max_length": DEFAULT_MAX_LENGTH,
        "placeholder": DEFAULT_PLACEHOLDER,
        "tts": True,
        "tts_voice": "2",
        "tts_template": "전투지원 요청, {content}",
        "tts_engine": "music",
        "tts_channel_ids": [],
        "default_combat_room_channel_id": None,
        "clans": {name: {"combat_room_channel_id": None} for name, _ in clan_roles},
    }


SECTION_COMMENT = (
    "/전투지원 설정. tts_channel_ids = 요청을 음성으로 읽어 줄 대화방(음성채널) ID 목록 — 적힌 개수만큼 읽어요 (1개면 1번, 2개면 2번). "
    "tts_engine = music이면 음악봇들이 대화방마다 하나씩 들어가 동시에 읽고(음악 재생 중인 봇은 재생 종료 후 사용, 봇이 모자라면 이어서 읽음), "
    "음악봇이 꺼져 있으면 TTS 봇이 한 곳씩 차례로 읽어요. tts로 적으면 항상 TTS 봇. "
    "clans.<혈맹 역할 이름>.combat_room_channel_id = 요청자가 그 혈맹이면 알림에 링크할 연합 카테고리의 전투방(음성채널) ID — 같은 연합이면 같은 ID를 넣으면 돼요. "
    "default_combat_room_channel_id = 혈맹 역할이 없는 요청자용 전투방. max_length = 내용 글자 수 제한, "
    "tts_template = 읽을 문장 ({content}=내용, {clan}=요청자 혈맹, {name}=요청자). 채널을 고친 뒤 보스봇을 다시 시작하세요."
)


def ensure_section(config, clan_roles) -> bool:
    """config에 combat_support 자리가 없거나 새 혈맹이 빠졌으면 채운다. 바뀌었으면 True."""
    changed = False
    sec = config.get("combat_support")
    if not isinstance(sec, dict):
        config["combat_support"] = default_section(clan_roles)
        changed = True
    else:
        for k, v in default_section(clan_roles).items():
            if k not in sec:
                sec[k] = v
                changed = True
        clans = sec.get("clans")
        if not isinstance(clans, dict):
            sec["clans"] = clans = {}
            changed = True
        for name, _ in clan_roles:
            if name not in clans:
                clans[name] = {"combat_room_channel_id": None}
                changed = True
    if "_combat_support_comment" not in config:
        config["_combat_support_comment"] = SECTION_COMMENT
        changed = True
    if ALERT_CHANNEL_KEY not in config:
        config[ALERT_CHANNEL_KEY] = None
        changed = True
    return changed


# 동훈님이 정한 채널 (2026-11-05) — 비어 있는 칸만 채운다
INITIAL_ALERT_CHANNEL = 1567766439123755028
INITIAL_COMBAT_ROOM = 1567388527442919586
INITIAL_TTS_CHANNELS = [1564868379259441222, 1567164976987111494, 1567522000744882227,
                        1567532312499855400, 1567550786890637505]


def apply_initial_channels(config):
    sec = config.setdefault("combat_support", {})
    if not _cid(config.get(ALERT_CHANNEL_KEY)):
        config[ALERT_CHANNEL_KEY] = INITIAL_ALERT_CHANNEL
    if not _cid(sec.get("default_combat_room_channel_id")):
        sec["default_combat_room_channel_id"] = INITIAL_COMBAT_ROOM
    if not [c for c in (sec.get("tts_channel_ids") or []) if _cid(c)]:
        sec["tts_channel_ids"] = list(INITIAL_TTS_CHANNELS)


class Settings:
    def __init__(self, config, clan_roles):
        sec = config.get("combat_support") if isinstance(config.get("combat_support"), dict) else {}
        self.enabled = bool(sec.get("enabled", True))
        try:
            self.max_length = max(1, min(100, int(sec.get("max_length") or DEFAULT_MAX_LENGTH)))
        except (TypeError, ValueError):
            self.max_length = DEFAULT_MAX_LENGTH
        self.placeholder = str(sec.get("placeholder") or DEFAULT_PLACEHOLDER)[:100]
        self.tts = bool(sec.get("tts", True))
        self.tts_voice = str(sec.get("tts_voice") or "2")
        self.engine = str(sec.get("tts_engine") or "music").strip().lower()
        self.tts_template = str(sec.get("tts_template") or "전투지원 요청, {content}")
        self.default_room = _cid(sec.get("default_combat_room_channel_id"))
        self.clan_roles = list(clan_roles)
        raw = sec.get("tts_channel_ids")
        raw = raw if isinstance(raw, list) else [raw]
        self.tts_rooms = [c for c in (_cid(v) for v in raw) if c]
        self.combat_rooms = {}
        for name, item in (sec.get("clans") or {}).items():
            if isinstance(item, dict):
                self.combat_rooms[name] = _cid(item.get("combat_room_channel_id"))
        self.alert_channel_id = _cid(config.get(ALERT_CHANNEL_KEY))

    def clan_of(self, role_names):
        """멤버 역할 이름들 → (혈맹 이름, 이모지) / 혈맹 역할이 없으면 None (여러 개면 앞쪽 혈맹)."""
        roles = set(role_names or ())
        for name, emoji in self.clan_roles:
            if name in roles:
                return name, emoji
        return None

    def combat_room_for(self, clan_name):
        return (self.combat_rooms.get(clan_name) if clan_name else None) or self.default_room

    def tts_channels(self) -> list:
        """요청을 읽어 줄 대화방 ID — 설정에 적힌 순서·개수 그대로 (1개면 1번, 2개면 2번)."""
        return list(self.tts_rooms)

    def tts_text(self, content, clan_name="", name="") -> str:
        try:
            text = self.tts_template.format(content=content, clan=clan_name or "", name=name or "")
        except (KeyError, IndexError, ValueError):
            text = f"전투지원 요청, {content}"
        return " ".join(text.split())


def clean_content(text, max_length=DEFAULT_MAX_LENGTH) -> str:
    """한 줄로 정리 + 글자 수 제한 (팝업이 막아도 한 번 더)."""
    return " ".join(str(text or "").split())[:max_length]


# ── 상태 저장 ────────────────────────────────────────────────────────────
class Store:
    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            if self.log:
                self.log.info(f"[전투지원] 상태 파일 읽기 실패: {e}")
            return {}

    def get(self, message_id):
        return self.load().get(str(message_id))

    def put(self, message_id, record):
        with self._lock:
            data = self.load()
            data[str(message_id)] = record
            if len(data) > KEEP_RECORDS:
                for k in sorted(data, key=lambda k: data[k].get("created_at", ""))[:len(data) - KEEP_RECORDS]:
                    data.pop(k, None)
            write_json_atomic(self.path, data, self.log)

    def update(self, message_id, fn):
        """fn(기록) 으로 기록을 고친다 (제자리 수정). 반환: 기록 / 없음 None."""
        with self._lock:
            data = self.load()
            rec = data.get(str(message_id))
            if rec is None:
                return None
            fn(rec)
            write_json_atomic(self.path, data, self.log)
            return rec

    def update_attendees(self, message_id, fn):
        """fn(참석자 목록) → 새 목록. 반환: (기록, 바뀌었는지) / 기록 없음 (None, False)."""
        with self._lock:
            data = self.load()
            rec = data.get(str(message_id))
            if rec is None:
                return None, False
            before = list(rec.get("attendees") or [])
            after = list(fn(list(before)))
            if after != before:
                rec["attendees"] = after
                write_json_atomic(self.path, data, self.log)
            return rec, after != before


def merge_attendees(current, add=(), remove=()) -> list:
    """순서 유지: 기존 → 새로 온 사람 뒤에. remove는 빼기."""
    out = [str(u) for u in current if str(u) not in {str(r) for r in remove}]
    for u in add:
        if str(u) not in out:
            out.append(str(u))
    return out


# ── 화면 ────────────────────────────────────────────────────────────────
def build_embed(rec, names, clan_split=None):
    """rec: 저장 기록, names: {uid: 이름}, clan_split: 참석자 UID → (groups, unassigned) 또는 None.
    B안 (동훈님 선택 2026-11-05): 제목 고정, 내용은 📌 줄. 전투종료 후엔 회색 + 🔒 종료 시각 + 최종 참석."""
    ended = bool(rec.get("ended_at"))
    embed = discord.Embed(color=0x888888 if ended else 0xE53935,
                          title="⚔️ 전투지원 요청" + (" (종료)" if ended else ""))
    clan = rec.get("clan") or ""
    who = rec.get("requester_name", "")
    if clan:
        who += f" ({(rec.get('clan_emoji') or '') + ' ' if rec.get('clan_emoji') else ''}{clan})"
    line = f"🙋 {who} · {rec.get('time', '')}"
    room = rec.get("combat_room_id")
    if room:
        line += f"  👉 <#{room}>"
    lines = [f"📌 {rec.get('content', '')}", line]
    if ended:
        lines.append(f"🔒 전투종료 {rec.get('ended_time', '')}".rstrip())
    prefix = "✅ 최종 참석" if ended else "✅ 현재 참석"
    attendees = [str(u) for u in rec.get("attendees") or []]
    split = None
    if clan_split is not None:
        try:
            split = clan_split(attendees)
        except Exception:
            split = None
    if split:
        lines.append(f"\n{prefix}: 총 {len(attendees)}명")
        embed.description = "\n".join(lines)
        for fname, fval in rb_clans.clan_fields(*split):
            embed.add_field(name=fname, value=fval, inline=False)
    else:
        people = " · ".join(names.get(u, u) for u in attendees) or "없음"
        lines.append(f"\n{prefix}: {len(attendees)}명 | {people}")
        embed.description = "\n".join(lines)[:4000]
    return embed


def push_text(rec) -> str:
    """폰 푸시 미리보기용 평문 (마크다운·채널 링크 없이)."""
    return f"⚔️ 전투지원 요청{' (종료)' if rec.get('ended_at') else ''}: {rec.get('content', '')}"


# ── 디스코드 ─────────────────────────────────────────────────────────────
class CombatSupport:
    """보스봇이 만들어 붙인다. helpers는 보스봇 함수 (관리자 판정·혈맹 분류·이름표)."""

    def __init__(self, bot, config, clan_roles, data_dir, *, is_admin, clan_split_ids, id_to_name,
                 update_user_mapping, tts_port=0, music_port=0, logger=None):
        self.bot = bot
        self.config = config
        self.settings = Settings(config, clan_roles)
        self.store = Store(os.path.join(data_dir, "combat_support.json"), logger)
        self.is_admin = is_admin
        self.clan_split_ids = clan_split_ids
        self.id_to_name = id_to_name
        self.update_user_mapping = update_user_mapping
        self.tts_port = tts_port
        self.music_port = music_port
        self.log = logger
        self._locks = defaultdict(asyncio.Lock)

    def _info(self, text):
        if self.log:
            self.log.info(text)

    def reload(self, clan_roles=None):
        self.settings = Settings(self.config, clan_roles or self.settings.clan_roles)

    # 등록 ---------------------------------------------------------------
    async def create(self, interaction: discord.Interaction, raw_content: str):
        s = self.settings
        content = clean_content(raw_content, s.max_length)
        if not content:
            await interaction.followup.send("내용을 적어 주세요.", ephemeral=True)
            return
        ch = self.bot.get_channel(s.alert_channel_id) if s.alert_channel_id else None
        if ch is None and s.alert_channel_id:
            try:
                ch = await self.bot.fetch_channel(s.alert_channel_id)
            except Exception:
                ch = None
        if ch is None:
            await interaction.followup.send(
                "⚠️ 전투알림 채널이 아직 설정되지 않았어요. 운영진이 boss_config.json의 "
                f"`{ALERT_CHANNEL_KEY}`에 채널 ID를 넣어 주세요.", ephemeral=True)
            return
        user = interaction.user
        clan = s.clan_of(r.name.strip() for r in getattr(user, "roles", []) or [])
        clan_name, clan_emoji = clan if clan else ("", "")
        name = getattr(user, "display_name", None) or user.name
        rec = {
            "channel_id": ch.id,
            "content": content,
            "requester_id": str(user.id),
            "requester_name": name,
            "clan": clan_name,
            "clan_emoji": clan_emoji,
            "combat_room_id": s.combat_room_for(clan_name),
            "time": datetime.now().strftime("%H:%M"),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "attendees": [],
        }
        self.update_user_mapping(str(user.id), name)
        msg = await ch.send(content=push_text(rec), embed=self._embed(rec), view=CombatView(self))
        self.store.put(msg.id, rec)
        self._info(f"[전투지원] 등록 msg:{msg.id} | {name}({clan_name or '혈맹 없음'}) | {content} | "
                   f"전투방 {rec['combat_room_id'] or '미설정'}")
        spoken = await self.announce(rec)
        note = f"✅ 전투지원 알림을 등록했어요 → {msg.jump_url}"
        if not rec["combat_room_id"]:
            note += "\n-# 전투방 채널 ID가 아직 없어 바로가기 링크는 빠졌어요."
        if s.tts and not spoken:
            note += "\n-# 대화방 음성 안내는 못 했어요 (tts_channel_ids 미설정 또는 음악봇·TTS 봇 꺼짐)."
        await interaction.followup.send(note, ephemeral=True)

    async def announce(self, rec) -> int:
        """설정된 대화방에 음성으로 요청을 읽어 준다. 반환: 안내를 보낸 대화방 수.
        1) 음악봇(뮤직봇들이 대화방마다 하나씩 동시에, 재생 중이면 재생 종료 후) 2) 안 되면 TTS 봇(한 곳씩 차례로)."""
        s = self.settings
        if not s.tts:
            return 0
        text = s.tts_text(rec.get("content", ""), rec.get("clan", ""), rec.get("requester_name", ""))
        return await self.speak(text, tag="전투지원")

    async def speak(self, text, tag="전투지원") -> int:
        """설정된 대화방(tts_channel_ids)에 text를 음성으로 읽는다 (/전파도 같이 씀). 반환: 안내한 대화방 수."""
        s = self.settings
        channels = s.tts_channels()
        if not channels or not text:
            return 0
        if s.engine == "music" and self.music_port:
            try:
                res = await send_ipc(self.music_port, "announce", [text, s.tts_voice, channels],
                                     timeout_connect=2, timeout_read=15)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            if res and res.get("ok"):
                return int(res.get("channels") or len(channels))
            self._info(f"[{tag} 음성] 음악봇 안내 실패 → TTS 봇으로: {(res or {}).get('error')}")
        if not self.tts_port:
            return 0
        ok = 0
        for cid in channels:
            try:
                res = await send_ipc(self.tts_port, "speak", [text, s.tts_voice, cid],
                                     timeout_connect=2, timeout_read=5)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            if res and res.get("ok"):
                ok += 1
            else:
                self._info(f"[{tag} TTS] {cid} 실패: {(res or {}).get('error')}")
        return ok

    # 화면 갱신 -----------------------------------------------------------
    def _embed(self, rec):
        return build_embed(rec, self.id_to_name(), self.clan_split_ids)

    async def redraw(self, message, rec):
        try:
            await message.edit(content=push_text(rec), embed=self._embed(rec),
                               view=CombatView(self, ended=bool(rec.get("ended_at"))))
        except Exception as e:
            self._info(f"[전투지원] 화면 갱신 실패 {getattr(message, 'id', '')}: {type(e).__name__}: {e}")

    # 인원체크 -------------------------------------------------------------
    def _voice_channels(self, guild, rec):
        """확인할 음성채널 — 알림에 링크된 전투방, 없으면 설정된 전투방 전체."""
        ids = [rec.get("combat_room_id")] if rec.get("combat_room_id") else []
        if not ids:
            ids = [c for c in list(self.settings.combat_rooms.values()) + [self.settings.default_room] if c]
        out = []
        for cid in dict.fromkeys(ids):
            ch = guild.get_channel(int(cid)) if guild else None
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                out.append(ch)
        return out

    async def voice_check(self, message):
        """전투방 접속자를 참석자에 더한다 (나간 사람은 빼지 않음 — 지원 온 기록 유지). 반환: (신규, 총원) / 문자열=안내."""
        async with self._locks[message.id]:
            rec = self.store.get(message.id)
            if rec is None:
                return "⚠️ 이 알림의 기록을 찾을 수 없어요."
            if rec.get("ended_at"):
                return "🔒 이미 전투종료된 알림이에요. 빠진 사람은 ➕ 인원 추가로 넣어 주세요."
            channels = self._voice_channels(message.guild, rec)
            if not channels:
                return "⚠️ 전투방 채널 ID가 아직 설정되지 않았어요 (boss_config.json의 combat_support)."
            found = {}
            for ch in channels:
                for m in ch.members:
                    if not m.bot:
                        found[str(m.id)] = m.display_name
            before = len(rec.get("attendees") or [])
            rec, _ = self.store.update_attendees(message.id, lambda cur: merge_attendees(cur, add=found))
            for uid, name in found.items():
                self.update_user_mapping(uid, name)
            total = len(rec.get("attendees") or [])
            self._info(f"[전투지원 인원체크] msg:{message.id} | 음성 {len(found)}명, 신규 {total - before}명, 총 {total}명")
            await self.redraw(message, rec)
            return total - before, total

    async def end(self, message, user):
        """⏹️ 전투종료 — 마지막으로 전투방 인원체크를 한 번 하고 종료 표시 (인원체크·전투종료 버튼 꺼짐, ➕/➖는 그대로).
        반환: 기록 / 문자열=안내."""
        rec = self.store.get(message.id)
        if rec is None:
            return "⚠️ 이 알림의 기록을 찾을 수 없어요."
        if rec.get("ended_at"):
            return "🔒 이미 전투종료된 알림이에요."
        try:
            await self.voice_check(message)   # 종료 직전 전투방 인원 담기 (채널 미설정이면 건너뜀)
        except Exception as e:
            self._info(f"[전투종료 전 인원체크 실패] {message.id}: {type(e).__name__}: {e}")
        async with self._locks[message.id]:
            now = datetime.now()

            def _mark(r):
                r.setdefault("ended_at", now.isoformat(timespec="seconds"))
                r.setdefault("ended_time", now.strftime("%H:%M"))
                r.setdefault("ended_by", str(user.id))
            rec = self.store.update(message.id, _mark)
            await self.redraw(message, rec)
        self._info(f"[전투종료] msg:{message.id} user:{user.id} | 최종 {len(rec.get('attendees') or [])}명")
        return rec

    def can_end(self, user, rec) -> bool:
        """전투종료: 요청한 사람 본인만 (권한 타입 '요청자', 2026-11-06 동훈님 — 운영진도 안 됨)."""
        return str(getattr(user, "id", "")) == str((rec or {}).get("requester_id"))

    async def edit_attendees(self, message, add=(), remove=()):
        async with self._locks[message.id]:
            rec, changed = self.store.update_attendees(
                message.id, lambda cur: merge_attendees(cur, add=add, remove=remove))
            if rec is not None and changed:
                await self.redraw(message, rec)
            return rec

    def clan_summary(self, rec) -> str:
        try:
            split = self.clan_split_ids([str(u) for u in rec.get("attendees") or []])
        except Exception:
            split = None
        return f" ({rb_clans.format_clan_counts(*split)})" if split else ""


class CombatModal(ui.Modal, title="⚔️ 전투지원 요청"):
    def __init__(self, cs: CombatSupport):
        super().__init__(timeout=300)
        self.cs = cs
        s = cs.settings
        self.content = ui.TextInput(label="지원 내용", placeholder=f"{s.placeholder} ({s.max_length}자)"[:100],
                                    min_length=1, max_length=s.max_length, required=True)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cs.create(interaction, self.content.value)
        except Exception as e:
            self.cs._info(f"[전투지원] 등록 실패: {type(e).__name__}: {e}")
            await interaction.followup.send(f"❌ 등록 실패: {e}", ephemeral=True)


class CombatView(rb_perms.GuardedView):
    """등록된 전투지원 알림 버튼 — custom_id 고정이라 재시작 후에도 동작."""

    def __init__(self, cs: CombatSupport, ended=False):
        super().__init__(timeout=None)
        self.cs = cs
        for child in self.children:
            if getattr(child, "custom_id", None) in (BTN_VOICECHECK, BTN_END):
                child.disabled = bool(ended)

    async def _admin_only(self, interaction, what):
        if self.cs.is_admin(interaction.user):
            return True
        await interaction.followup.send(f"⛔ {what}는 운영진만 할 수 있어요.", ephemeral=True)
        return False

    @ui.button(label="인원체크", style=discord.ButtonStyle.success, custom_id=BTN_VOICECHECK)
    async def voice_check_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not await self._admin_only(interaction, "인원체크"):
            return
        try:
            result = await self.cs.voice_check(interaction.message)
        except Exception as e:
            self.cs._info(f"[전투지원 인원체크 실패] {interaction.message.id}: {type(e).__name__}: {e}")
            result = "⚠️ 인원체크 중 오류가 발생했어요."
        if isinstance(result, str):
            text = result
        else:
            added, total = result
            rec = self.cs.store.get(interaction.message.id) or {}
            text = f"🎙️ 전투방 인원체크 완료 — 신규 {added}명, 총 {total}명{self.cs.clan_summary(rec)}"
        await interaction.followup.send(text, ephemeral=True)

    @ui.button(label="인원 추가", emoji="➕", style=discord.ButtonStyle.secondary, custom_id=BTN_ADD)
    async def add_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not await self._admin_only(interaction, "인원 추가"):
            return
        await interaction.followup.send("➕ 추가할 멤버를 검색해서 고르세요.",
                                        view=_AddPicker(self.cs, interaction.message), ephemeral=True)

    @ui.button(label="인원 제거", emoji="➖", style=discord.ButtonStyle.secondary, custom_id=BTN_REMOVE)
    async def remove_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not await self._admin_only(interaction, "인원 제거"):
            return
        rec = self.cs.store.get(interaction.message.id)
        if not rec or not rec.get("attendees"):
            await interaction.followup.send("참석자가 없어요.", ephemeral=True)
            return
        await interaction.followup.send("➖ 제거할 참석자를 고르세요.",
                                        view=_RemovePicker(self.cs, interaction.message, rec), ephemeral=True)


    @ui.button(label="전투종료", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id=BTN_END)
    async def end_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        rec = self.cs.store.get(interaction.message.id)
        if not self.cs.can_end(interaction.user, rec):
            await interaction.followup.send("⛔ 전투종료는 요청한 사람만 할 수 있어요.", ephemeral=True)
            return
        try:
            result = await self.cs.end(interaction.message, interaction.user)
        except Exception as e:
            self.cs._info(f"[전투종료 실패] {interaction.message.id}: {type(e).__name__}: {e}")
            result = "⚠️ 전투종료 중 오류가 발생했어요."
        if isinstance(result, str):
            text = result
        else:
            text = (f"⏹️ 전투종료 — 최종 참석 {len(result.get('attendees') or [])}명"
                    f"{self.cs.clan_summary(result)}")
        await interaction.followup.send(text, ephemeral=True)


class _AddPicker(ui.View):
    def __init__(self, cs, message):
        super().__init__(timeout=180)
        self.cs, self.message = cs, message
        sel = ui.UserSelect(placeholder="추가할 멤버 이름을 입력해 검색하세요", min_values=1, max_values=25)
        sel.callback = self._picked
        self.add_item(sel)
        self.sel = sel

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rec = self.cs.store.get(self.message.id) or {}
        have = {str(u) for u in rec.get("attendees") or []}
        targets, skipped = [], []
        for m in self.sel.values:
            name = getattr(m, "display_name", None) or m.name
            if getattr(m, "bot", False) or str(m.id) in have:
                skipped.append(name)
                continue
            self.cs.update_user_mapping(str(m.id), name)
            targets.append((str(m.id), name))
        if not targets:
            await interaction.edit_original_response(
                content=f"⚠️ 이미 참석자예요: {', '.join(skipped)}" if skipped else "추가할 멤버가 없어요.", view=None)
            return
        await self.cs.edit_attendees(self.message, add=[u for u, _ in targets])
        self.cs._info(f"[전투지원 인원 추가] msg:{self.message.id} user:{interaction.user.id} | {[u for u, _ in targets]}")
        text = f"✅ 참석자에 추가: {', '.join(n for _, n in targets)}"
        if skipped:
            text += f"\n-# 이미 참석자라 건너뜀: {', '.join(skipped)}"
        await interaction.edit_original_response(content=text, view=None)
        self.stop()


class _RemovePicker(ui.View):
    def __init__(self, cs, message, rec, page=0):
        super().__init__(timeout=180)
        self.cs, self.message, self.rec = cs, message, rec
        id2n = cs.id_to_name()
        people = sorted(((str(u), id2n.get(str(u), str(u))) for u in rec.get("attendees") or []), key=lambda x: x[1])
        self.people = people
        pages = max(1, (len(people) + PICK_PAGE - 1) // PICK_PAGE)
        self.page = min(page, pages - 1)
        chunk = people[self.page * PICK_PAGE:(self.page + 1) * PICK_PAGE]
        sel = ui.Select(placeholder="제거할 참석자를 고르세요" + (f" ({self.page + 1}/{pages})" if pages > 1 else ""),
                        min_values=1, max_values=len(chunk),
                        options=[discord.SelectOption(label=name[:100], value=uid) for uid, name in chunk])
        sel.callback = self._picked
        self.add_item(sel)
        self.sel = sel
        if pages > 1:
            for label, delta in (("◀ 이전", -1), ("다음 ▶", 1)):
                b = ui.Button(label=label, style=discord.ButtonStyle.secondary,
                              disabled=not (0 <= self.page + delta < pages))
                b.callback = self._pager(delta)
                self.add_item(b)

    def _pager(self, delta):
        async def go(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=_RemovePicker(self.cs, self.message, self.rec, self.page + delta))
            self.stop()
        return go

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        names = dict(self.people)
        targets = list(self.sel.values)
        rec = await self.cs.edit_attendees(self.message, remove=targets)
        if rec is None:
            await interaction.edit_original_response(content="⚠️ 이 알림의 기록을 찾을 수 없어요.", view=None)
            return
        self.cs._info(f"[전투지원 인원 제거] msg:{self.message.id} user:{interaction.user.id} | {targets}")
        await interaction.edit_original_response(
            content=f"✅ 참석자에서 제거: {', '.join(names.get(u, u) for u in targets)}", view=None)
        self.stop()
