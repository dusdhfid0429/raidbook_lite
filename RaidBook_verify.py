# -*- coding: utf-8 -*-
"""🔐 서버 입장 인증 — #인증 채널의 버튼 → 비밀번호 입력창 → 맞으면 '인증됨' 역할.

입력 내용과 결과는 누른 사람에게만 보인다 (모달 + ephemeral).
틀린 횟수는 Data/verify_state.json 에 남기고, max_failures 번 틀리면 추방(fail_action=kick)한다.

설정 (boss_config.json):
  verify_channel_id        #인증 채널 ID (비면 RaidBook 카테고리에 '인증' 채널 자동 생성)
  verify.enabled           켜기/끄기
  verify.password          입장 비밀번호 (비면 인증이 안 되고 '운영진 문의' 안내)
  verify.role_id           인증되면 줄 역할 ID (비면 role_name 역할을 찾고, 없으면 만들어 ID 저장)
  verify.role_name         역할 이름 (기본 '인증됨')
  verify.max_failures      이 횟수만큼 틀리면 추방 (0 = 제한 없음)
  verify.fail_action       kick = 추방, none = 안내만
  verify.log_channel_id    인증·추방 기록 채널 (비면 보탐-명령 채널)
"""
import asyncio
import hmac
import json
import os

import discord

import RaidBook_perms as rb_perms

from RaidBook_common import write_json_atomic

CHANNEL_KEY = "verify_channel_id"
CHANNEL_NAME = "인증"
DEFAULT_ROLE_NAME = "인증됨"
DEFAULT_MAX_FAILURES = 5
BUTTON_ID = "rb_verify:open"
STATE_FILE = "verify_state.json"

SECTION_COMMENT = (
    "🔐 서버 입장 인증. verify_channel_id = #인증 채널 ID(비면 '인증' 채널 자동 생성), "
    "verify.password = 입장 비밀번호(비면 인증 안 됨), verify.role_id = 인증되면 줄 역할 ID"
    "(비면 role_name 역할을 찾거나 새로 만들어 저장), verify.max_failures = 이 횟수 틀리면 추방(0=제한 없음), "
    "verify.fail_action = kick(추방)/none(안내만), verify.log_channel_id = 기록 채널(비면 보탐-명령), "
    "verify.keep_role_name = /인증권한 때 채널 권한을 남길 역할 이름(기본 운영진, 비우면 전부 지움). "
    "봇에 '역할 관리'·'멤버 추방' 권한이 있어야 하고, 봇 역할이 '인증됨'보다 위에 있어야 해요. "
    "@everyone은 #인증만 보이게, 일반 채널은 '인증됨'만 보이게 서버 설정에서 바꿔 주세요. 고친 뒤 보스봇을 다시 시작하세요."
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
        "password": "",
        "role_id": None,
        "role_name": DEFAULT_ROLE_NAME,
        "max_failures": DEFAULT_MAX_FAILURES,
        "fail_action": "kick",
        "log_channel_id": None,
        "keep_role_name": "운영진",
    }


def ensure_section(config) -> bool:
    """verify 자리·verify_channel_id 가 없거나 빠진 칸이 있으면 채운다. 바뀌었으면 True."""
    changed = False
    sec = config.get("verify")
    if not isinstance(sec, dict):
        config["verify"] = default_section()
        changed = True
    else:
        # 예전 keep_role_suffix('운영진'으로 끝나는 역할) → keep_role_name('운영진' 역할 하나) (2026-11-06)
        if "keep_role_suffix" in sec:
            sec.pop("keep_role_suffix")
            sec["keep_role_name"] = "운영진"
            changed = True
        for k, v in default_section().items():
            if k not in sec:
                sec[k] = v
                changed = True
    if CHANNEL_KEY not in config:
        config[CHANNEL_KEY] = None
        changed = True
    if config.get("_verify_comment") != SECTION_COMMENT:
        config["_verify_comment"] = SECTION_COMMENT
        changed = True
    return changed


class Settings:
    def __init__(self, config):
        sec = config.get("verify") if isinstance(config.get("verify"), dict) else {}
        self.enabled = bool(sec.get("enabled", True))
        self.password = str(sec.get("password") or "").strip()
        self.role_id = _cid(sec.get("role_id"))
        self.role_name = str(sec.get("role_name") or DEFAULT_ROLE_NAME).strip() or DEFAULT_ROLE_NAME
        try:
            self.max_failures = max(0, int(sec.get("max_failures", DEFAULT_MAX_FAILURES)))
        except (TypeError, ValueError):
            self.max_failures = DEFAULT_MAX_FAILURES
        self.kick = str(sec.get("fail_action") or "kick").strip().lower() == "kick"
        self.log_channel_id = _cid(sec.get("log_channel_id"))
        self.channel_id = _cid(config.get(CHANNEL_KEY))
        self.keep_role_name = str(sec.get("keep_role_name") if sec.get("keep_role_name") is not None
                                  else "운영진").strip()

    def check(self, typed) -> bool:
        """비밀번호 비교 (앞뒤 공백 무시, 대소문자 구분). 비밀번호가 비어 있으면 항상 False."""
        if not self.password:
            return False
        return hmac.compare_digest(str(typed or "").strip().encode("utf-8"), self.password.encode("utf-8"))


class FailureStore:
    """사용자별 틀린 횟수와 #인증 안내 메시지 ID를 Data/verify_state.json 에 보관."""

    def __init__(self, data_dir, logger=None):
        self.path = os.path.join(data_dir, STATE_FILE)
        self.logger = logger
        self.data = {"failures": {}, "panel": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data["failures"] = dict(loaded.get("failures") or {})
                self.data["panel"] = dict(loaded.get("panel") or {})
        except FileNotFoundError:
            pass
        except Exception as e:
            if logger:
                logger.warning(f"[인증] {STATE_FILE} 읽기 실패 — 새로 시작: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            write_json_atomic(self.path, self.data, self.logger)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[인증] {STATE_FILE} 저장 실패: {e}")

    def count(self, uid) -> int:
        return int(self.data["failures"].get(str(uid), 0) or 0)

    def add_failure(self, uid) -> int:
        n = self.count(uid) + 1
        self.data["failures"][str(uid)] = n
        self._save()
        return n

    def reset(self, uid):
        if self.data["failures"].pop(str(uid), None) is not None:
            self._save()

    def panel(self):
        p = self.data.get("panel") or {}
        return _cid(p.get("channel_id")), _cid(p.get("message_id"))

    def set_panel(self, channel_id, message_id):
        self.data["panel"] = {"channel_id": channel_id, "message_id": message_id}
        self._save()


# ── 화면 문구 (B안: 안내형) ──────────────────────────────────────────────

PANEL_TITLE = "🔐 서버 입장 인증"
BUTTON_LABEL = "비밀번호 입력"
BUTTON_EMOJI = "🔑"


def panel_description(settings: Settings) -> str:
    lines = ["1. 아래 버튼을 눌러 비밀번호를 입력하세요",
             "2. 맞으면 모든 채널이 열려요"]
    if settings.max_failures > 0 and settings.kick:
        lines.append(f"⚠️ {settings.max_failures}회 틀리면 서버에서 내보내져요")
    lines.append("비밀번호는 운영진에게 문의하세요")
    return "\n".join(lines)


MSG_OK = "✅ 인증 완료! 이제 모든 채널을 볼 수 있어요."
MSG_ALREADY = "✅ 이미 인증된 상태예요."
MSG_NO_PASSWORD = "⚠️ 아직 비밀번호가 설정되지 않았어요. 운영진에게 문의해 주세요."
MSG_DISABLED = "인증 기능이 꺼져 있어요."
MSG_ROLE_MISSING = "⚠️ 인증 역할을 찾지 못했어요. 운영진에게 문의해 주세요."
MSG_ROLE_FAIL = "⚠️ 비밀번호는 맞지만 역할을 주지 못했어요 (봇 권한 확인 필요). 운영진에게 문의해 주세요."


def wrong_message(failures, settings: Settings) -> str:
    if settings.max_failures <= 0:
        return "❌ 비밀번호가 틀렸어요."
    left = max(0, settings.max_failures - failures)
    return f"❌ 비밀번호가 틀렸어요. (남은 기회 {left}회)"


def kicked_message(settings: Settings) -> str:
    return f"⛔ {settings.max_failures}회 틀려서 서버에서 내보내졌어요."


def locked_message(settings: Settings) -> str:
    """추방하지 않는 설정(fail_action=none)에서 횟수를 다 쓴 경우."""
    return f"⛔ {settings.max_failures}회 틀려서 더 시도할 수 없어요. 운영진에게 문의해 주세요."


def log_ok(name) -> str:
    return f"🔐 {name} 인증 완료"


def log_kicked(name, n) -> str:
    return f"⛔ {name} {n}회 실패로 추방"


def is_exhausted(failures, settings: Settings) -> bool:
    return settings.max_failures > 0 and failures >= settings.max_failures


# ── Discord 연결 ─────────────────────────────────────────────────────────
EXISTING_FLAG = "_verify_existing_granted"


class Verifier:
    """보스봇이 하나 만들어 쓰는 인증 담당. save_config()는 config 파일 저장 콜백."""

    def __init__(self, bot, config, data_dir, *, save_config, default_log_channel=None, logger=None):
        self.bot = bot
        self.config = config
        self.save_config = save_config
        self.default_log_channel = default_log_channel or (lambda: None)
        self.logger = logger
        self.store = FailureStore(data_dir, logger)
        self.settings = Settings(config)
        self._locks = {}

    def reload(self):
        self.settings = Settings(self.config)

    def _log(self, msg, warn=False):
        if self.logger:
            (self.logger.warning if warn else self.logger.info)(msg)

    async def _post_log(self, text):
        cid = self.settings.log_channel_id or self.default_log_channel()
        ch = self.bot.get_channel(int(cid)) if cid else None
        if ch is None:
            return
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            self._log(f"[인증] 기록 채널 전송 실패: {e}", warn=True)

    def find_role(self, guild):
        if guild is None:
            return None
        if self.settings.role_id:
            role = guild.get_role(self.settings.role_id)
            if role:
                return role
        return discord.utils.get(guild.roles, name=self.settings.role_name)

    async def ensure_role(self, guild):
        """역할을 찾고, 없으면 만든다. ID가 바뀌면 config에 저장. 문제 안내 문자열 목록 반환."""
        notes = []
        role = self.find_role(guild)
        if role is None:
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                return None, [f"⚠️ '역할 관리' 권한이 없어 '{self.settings.role_name}' 역할을 만들지 못했어요 (인증 기능)"]
            try:
                role = await guild.create_role(name=self.settings.role_name, reason="RaidBook 서버 입장 인증")
                notes.append(f"🆕 '{role.name}' 역할을 만들었어요 (인증 기능)")
                self._log(f"[인증] 역할 생성 {role.name}({role.id})")
            except Exception as e:
                return None, [f"⚠️ '{self.settings.role_name}' 역할 생성 실패: {e}"]
        if self.settings.role_id != role.id:
            self.config.setdefault("verify", {})["role_id"] = role.id
            self.save_config()
            self.reload()
        return role, notes

    async def grant_bots(self, guild, role):
        """봇들(보스·음악·TTS)도 인증 역할을 받아야 /인증권한 뒤에도 채널을 볼 수 있다 — 시작할 때마다 확인."""
        if role is None:
            return
        for m in guild.members:
            if m.bot and role not in m.roles:
                try:
                    await m.add_roles(role, reason="RaidBook 인증 — 봇은 항상 인증")
                except Exception as e:
                    self._log(f"[인증] 봇 역할 부여 실패 {m}: {e}", warn=True)

    async def grant_existing(self, guild, role):
        """처음 한 번만 — 지금 서버에 있는 사람(봇 제외) 모두에게 인증 역할을 준다."""
        if self.config.get(EXISTING_FLAG) or role is None:
            return []
        targets = [m for m in guild.members if not m.bot and role not in m.roles]
        ok = fail = 0
        for m in targets:
            try:
                await m.add_roles(role, reason="RaidBook 인증 도입 — 기존 멤버 일괄 인증")
                ok += 1
            except discord.Forbidden:
                fail += 1
            except Exception as e:
                fail += 1
                self._log(f"[인증] 기존 멤버 역할 부여 실패 {m}: {e}", warn=True)
            await asyncio.sleep(0.3)
        if fail and not ok and targets:
            # 권한 문제로 전부 실패 — 다음 시작 때 다시 시도
            return [f"⚠️ 기존 멤버 {fail}명에게 '{role.name}' 역할을 주지 못했어요 (봇 역할 순서·'역할 관리' 권한 확인). 다음 시작 때 다시 시도해요."]
        self.config[EXISTING_FLAG] = True
        self.save_config()
        self._log(f"[인증] 기존 멤버 일괄 인증 {ok}명 (실패 {fail})")
        note = f"🔐 기존 멤버 {ok}명에게 '{role.name}' 역할을 줬어요"
        return [note + (f" (실패 {fail}명)" if fail else "")]

    def build_embed(self):
        return discord.Embed(title=PANEL_TITLE, description=panel_description(self.settings),
                             color=discord.Color.blurple())

    async def ensure_panel(self):
        """#인증 채널에 안내 메시지를 하나 유지 (있으면 내용만 고침, 없으면 새로 올림)."""
        cid = self.settings.channel_id
        ch = self.bot.get_channel(cid) if cid else None
        if ch is None:
            return
        embed, view = self.build_embed(), VerifyView(self)
        old_cid, old_mid = self.store.panel()
        if old_cid == cid and old_mid:
            try:
                msg = await ch.fetch_message(old_mid)
                await msg.edit(content=None, embed=embed, view=view)
                return
            except discord.NotFound:
                pass
            except Exception as e:
                self._log(f"[인증] 안내 메시지 갱신 실패: {e}", warn=True)
                return
        try:
            msg = await ch.send(embed=embed, view=view)
            self.store.set_panel(cid, msg.id)
            self._log(f"[인증] #{ch.name}에 안내 메시지를 올렸어요 ({msg.id})")
        except Exception as e:
            self._log(f"[인증] 안내 메시지 게시 실패: {e}", warn=True)

    async def setup(self, guild):
        """보스봇 시작 시: 역할 확인 → 기존 멤버 일괄 인증(1회) → 안내 메시지. 안내 문자열 목록 반환."""
        self.reload()
        if not self.settings.enabled or guild is None:
            return []
        role, notes = await self.ensure_role(guild)
        notes += await self.grant_existing(guild, role)
        await self.grant_bots(guild, role)
        await self.ensure_panel()
        if not self.settings.password:
            notes.append("⚠️ 인증 비밀번호가 비어 있어요 — boss_config.json의 verify.password를 넣고 보스봇을 다시 시작하세요")
        return notes

    async def submit(self, interaction, typed):
        s = self.settings
        user = interaction.user
        guild = interaction.guild
        if not s.enabled:
            await interaction.response.send_message(MSG_DISABLED, ephemeral=True)
            return
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("서버 안에서만 쓸 수 있어요.", ephemeral=True)
            return
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            role = self.find_role(guild)
            if role is None:
                await interaction.response.send_message(MSG_ROLE_MISSING, ephemeral=True)
                return
            if role in user.roles:
                await interaction.response.send_message(MSG_ALREADY, ephemeral=True)
                return
            if not s.password:
                await interaction.response.send_message(MSG_NO_PASSWORD, ephemeral=True)
                return
            if is_exhausted(self.store.count(user.id), s) and not s.kick:
                await interaction.response.send_message(locked_message(s), ephemeral=True)
                return
            name = user.display_name
            if s.check(typed):
                try:
                    await user.add_roles(role, reason="RaidBook 서버 입장 인증")
                except Exception as e:
                    self._log(f"[인증] 역할 부여 실패 {name}({user.id}): {e}", warn=True)
                    await interaction.response.send_message(MSG_ROLE_FAIL, ephemeral=True)
                    return
                self.store.reset(user.id)
                await interaction.response.send_message(MSG_OK, ephemeral=True)
                self._log(f"[인증] 완료 {name}({user.id})")
                await self._post_log(log_ok(name))
                return
            n = self.store.add_failure(user.id)
            self._log(f"[인증] 실패 {name}({user.id}) {n}회")
            if not is_exhausted(n, s):
                await interaction.response.send_message(wrong_message(n, s), ephemeral=True)
                return
            if not s.kick:
                await interaction.response.send_message(locked_message(s), ephemeral=True)
                return
            await interaction.response.send_message(kicked_message(s), ephemeral=True)
            try:
                await user.kick(reason=f"RaidBook 인증 {n}회 실패")
                self.store.reset(user.id)   # 다시 들어오면 처음부터
                self._log(f"[인증] {n}회 실패로 추방 {name}({user.id})")
                await self._post_log(log_kicked(name, n))
            except Exception as e:
                self._log(f"[인증] 추방 실패 {name}({user.id}): {e}", warn=True)
                await self._post_log(f"⚠️ {name} {n}회 실패 — 추방 권한이 없어 내보내지 못했어요")


class VerifyModal(discord.ui.Modal, title="서버 인증"):
    def __init__(self, verifier):
        super().__init__(timeout=300)
        self.verifier = verifier
        self.password = discord.ui.TextInput(label="비밀번호", placeholder="운영진에게 받은 비밀번호",
                                             max_length=100, required=True)
        self.add_item(self.password)

    async def on_submit(self, interaction):
        await self.verifier.submit(interaction, self.password.value)


class VerifyView(rb_perms.GuardedView):
    def __init__(self, verifier):
        super().__init__(timeout=None)
        self.verifier = verifier

    @discord.ui.button(label=BUTTON_LABEL, emoji=BUTTON_EMOJI, style=discord.ButtonStyle.primary,
                       custom_id=BUTTON_ID)
    async def open_modal(self, interaction, button):
        if not self.verifier.settings.enabled:
            await interaction.response.send_message(MSG_DISABLED, ephemeral=True)
            return
        await interaction.response.send_modal(VerifyModal(self.verifier))
