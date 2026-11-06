# -*- coding: utf-8 -*-
"""🛡️ 기능별 권한 — 모든 프로그램(보스봇·음악봇·TTS봇·Watchdog)의 명령·버튼을 권한 타입과 함께 표(데이터)로
등록하고, 들어오면 이 표부터 확인한다.

권한 타입
  NONE         제한 없음 — 누구나 (편의 기능)
  운영진       '운영진' 역할(admin_role_names·admin_role_ids), 서버마스터, 또는 서버 주인
               (정도운영진·슈퍼맨운영진 같은 ○○운영진 역할은 표시용이라 아님)
  서버마스터   '서버마스터' 역할(master_role_names·master_role_ids) 또는 서버 주인 — 프로그램을 직접 조작하는 기능
               (재시작·저장·스케줄·데이터 수정·/인증권한·Watchdog). 서버마스터는 운영진 기능도 모두 쓸 수 있음
  요청자       누구나 들어오고, 버튼 안에서 요청한 본인인지 확인 — 운영진도 안 됨 (전투지원 ⏹️ 전투종료)
  참석자       누구나 들어오고, 버튼 안에서 그 보스 참석자인지 확인 (입찰)

입구
  - 슬래시 명령: 각 봇의 CommandTree.interaction_check → check_command(interaction, bot)
  - ! 명령(Watchdog): bot.check → allowed(command_level(name, "watchdog"), author)
  - 버튼·선택: 상시 버튼 View 의 공용 부모 GuardedView.interaction_check → check_component
표에 없는 명령은 그 프로세스 명령이 아니라고 보고 통과(보스봇·음악봇이 토큰을 같이 쓰면 서로의 명령이 들어옴).
표에 없는 버튼은 GuardedView.default_level 로 판정(1회용 화면).
boss_config.json 의 permission_overrides 로 타입을 바꿀 수 있다 (예: {"전투지원": "운영진", "재생": "NONE"}).
"""
import json

import discord

NONE = "NONE"
STAFF = "운영진"
OWNER = "서버마스터"   # 프로그램 관리자 (이름은 예전 그대로 OWNER)
REQUESTER = "요청자"
ATTENDEE = "참석자"
LEVELS = (NONE, STAFF, OWNER, REQUESTER, ATTENDEE)
_ALIASES = {"누구나": NONE, "none": NONE, "서버주인": OWNER, "주인": OWNER}

STAFF_ROLE_NAME = "운영진"
MASTER_ROLE_NAME = "서버마스터"

# ── 명령 (프로그램 → 이름 → 타입) ─────────────────────────────────────────
COMMANDS = {
    "boss": {
        # NONE
        "전투지원": NONE,
        # 운영진 — 보스탐 운영
        "보스알림": STAFF, "전파": STAFF, "인원체크": STAFF, "인원현황": STAFF,
        "create": STAFF, "close": STAFF, "activate": STAFF, "extend": STAFF, "reopen": STAFF,
        "add": STAFF, "remove": STAFF,
        "listboss": STAFF, "listdailyboss": STAFF, "history": STAFF, "stats": STAFF,
        # 서버마스터 — 프로그램 조작 (스케줄·저장·재시작·데이터 수정·서버 권한)
        "startboss": OWNER, "stopboss": OWNER, "setboss": OWNER,
        "startdailyboss": OWNER, "stopdailyboss": OWNER, "setrefresh": OWNER,
        "refresh": OWNER, "save": OWNER, "restart": OWNER, "test": OWNER, "delete": OWNER,
        "recover": OWNER, "sync": OWNER, "fixid": OWNER, "fixnames": OWNER, "fixmsg": OWNER,
        "rename": OWNER, "attachloot": OWNER, "collectmembers": OWNER,
        "인증권한": OWNER,
    },
    "music": {
        "재생": NONE, "건너뛰기": NONE, "일시정지": NONE, "정지": NONE,
        "대기열": NONE, "현재곡": NONE, "삭제": NONE, "볼륨": NONE,
    },
    "tts": {
        "tts": NONE, "tts나가기": NONE, "tts스킵": NONE, "tts대기열": NONE,
    },
    "watchdog": {
        "watchdog": OWNER, "boss": OWNER, "panel": OWNER, "purgebot": OWNER,
    },
}

# ── 상시 버튼·선택 (custom_id → (타입, 이름)) ──────────────────────────────
COMPONENTS = {
    # 보스알림
    "boss_voicecheck": (STAFF, "인원체크"),
    "boss_attendee_add": (STAFF, "인원 추가"),
    "boss_attendee_remove": (STAFF, "인원 제거"),
    "boss_loot": (STAFF, "득템 기록"),
    "boss_auction_create": (STAFF, "경매생성"),
    "boss_party": (NONE, "파티 확인"),
    # 경매장
    "boss_board_start": (STAFF, "경매시작"),
    "boss_board_close": (STAFF, "경매 마감"),
    "boss_board_stop": (STAFF, "경매 종료"),
    "boss_board_relist": (STAFF, "유찰 재경매"),
    "boss_board_settle": (STAFF, "정산"),
    "boss_board_bid": (ATTENDEE, "입찰"),
    "boss_auc_start": (STAFF, "경매시작"),
    "boss_auc_stop": (STAFF, "경매 종료"),
    "boss_auc_bid": (ATTENDEE, "입찰"),
    "boss_loot_auction": (STAFF, "경매시작"),
    # 📌 경매 대기 목록
    "boss_pending_pick": (STAFF, "대기 목록에서 경매 만들기"),
    "boss_pending_hide": (STAFF, "목록에서 빼기"),
    "boss_pending_refresh": (NONE, "새로고침"),
    # 전투지원
    "combat_voicecheck": (STAFF, "인원체크"),
    "combat_attendee_add": (STAFF, "인원 추가"),
    "combat_attendee_remove": (STAFF, "인원 제거"),
    "combat_end": (REQUESTER, "전투종료"),
    # 🔐 #인증
    "rb_verify:open": (NONE, "비밀번호 입력"),
    # 🎵 음악 패널
    "rb_music:pause": (NONE, "일시정지"),
    "rb_music:skip": (NONE, "건너뛰기"),
    "rb_music:stop": (NONE, "정지"),
    "rb_music:queue": (NONE, "대기열"),
    "rb_music:voldown": (NONE, "볼륨 낮추기"),
    "rb_music:volup": (NONE, "볼륨 높이기"),
    # 🔊 TTS
    "rbtts:replay": (NONE, "다시 듣기"),
    # 🖥️ Watchdog 원격 패널
    "wd_panel_status": (OWNER, "상태"),
    "wd_panel_start": (OWNER, "시작"),
    "wd_panel_restart": (OWNER, "재시작"),
    "wd_panel_stop": (OWNER, "중지"),
    "wd_panel_logs": (OWNER, "최근 로그"),
    "wd_panel_update": (OWNER, "업데이트"),
}

_admin_role_names = {STAFF_ROLE_NAME}
_admin_role_ids = set()
_master_role_names = {MASTER_ROLE_NAME}
_master_role_ids = set()
_overrides = {}


def _level_name(value):
    v = str(value).strip()
    v = _ALIASES.get(v, _ALIASES.get(v.lower(), v))
    return v if v in LEVELS else None


def configure(config):
    """시작 때 한 번 — boss_config 의 admin_role_names·admin_role_ids·master_role_names·master_role_ids·
    permission_overrides 를 읽는다."""
    global _admin_role_names, _admin_role_ids, _master_role_names, _master_role_ids, _overrides
    config = config if isinstance(config, dict) else {}

    def _names(key, default):
        return {str(n).strip() for n in (config.get(key, [default]) or []) if str(n).strip()}

    def _ids(key):
        out = set()
        for r in config.get(key) or []:
            try:
                out.add(int(r))
            except (TypeError, ValueError):
                pass
        return out
    _admin_role_names, _admin_role_ids = _names("admin_role_names", STAFF_ROLE_NAME), _ids("admin_role_ids")
    _master_role_names, _master_role_ids = _names("master_role_names", MASTER_ROLE_NAME), _ids("master_role_ids")
    raw = config.get("permission_overrides")
    _overrides = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            lv = _level_name(v)
            if lv and not str(k).startswith("_"):
                _overrides[str(k)] = lv


def configure_from_file(path, logger=None):
    """음악봇·TTS봇·Watchdog 처럼 다른 프로세스는 boss_config.json 을 읽어 같은 기준을 쓴다 (없으면 기본값)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            configure(json.load(f))
    except Exception as e:
        configure({})
        if logger:
            logger.info(f"[권한] boss_config 읽기 실패 — 기본 권한 표로 동작: {e}")


def _is_guild_owner(member) -> bool:
    guild = getattr(member, "guild", None)
    return guild is not None and getattr(member, "id", None) == guild.owner_id


def _has_role(member, names, ids) -> bool:
    return any(r.id in ids or str(r.name).strip() in names for r in getattr(member, "roles", []) or [])


def is_master(member) -> bool:
    """서버마스터(프로그램 관리자): '서버마스터' 역할(master_role_names·master_role_ids) 또는 서버 주인."""
    if not isinstance(member, discord.Member):
        return False
    return _is_guild_owner(member) or _has_role(member, _master_role_names, _master_role_ids)


is_owner = is_master   # 예전 이름 호환


def is_staff(member) -> bool:
    """운영진: '운영진' 역할(admin_role_names·admin_role_ids), 서버마스터, 또는 서버 주인."""
    if not isinstance(member, discord.Member):
        return False
    return is_master(member) or _has_role(member, _admin_role_names, _admin_role_ids)


def command_level(name, bot="boss"):
    """명령 타입. 그 프로그램 표에 없으면 None (이 프로세스 명령 아님)."""
    table = COMMANDS.get(bot, {})
    if name not in table:
        return None
    return _overrides.get(name, table[name])


def component_level(custom_id):
    """버튼 (타입, 이름). 표에 없으면 (None, '')."""
    if custom_id not in COMPONENTS:
        return None, ""
    level, what = COMPONENTS[custom_id]
    return _overrides.get(custom_id, level), what


def allowed(level, member) -> bool:
    """이 타입을 이 사람이 바로 쓸 수 있는지. 요청자/참석자는 입구에서 통과시키고 버튼 안에서 마저 확인."""
    if level is None or level in (NONE, REQUESTER, ATTENDEE):
        return True
    if level == STAFF:
        return is_staff(member)
    if level == OWNER:
        return is_master(member)
    return False


def deny_text(level, what) -> str:
    who = "서버마스터" if level == OWNER else "운영진"
    return f"⛔ {what}은(는) {who}만 쓸 수 있어요."


async def _deny(interaction, text):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def check_command(interaction, bot="boss") -> bool:
    """슬래시 명령 입구. 막으면 본인에게만 안내하고 False (자동완성은 안내 없이 False)."""
    name = (interaction.data or {}).get("name") or ""
    level = command_level(name, bot)
    if allowed(level, interaction.user):
        return True
    if interaction.type == discord.InteractionType.application_command:
        await _deny(interaction, deny_text(level, f"/{name}"))
    return False


def command_gate(bot):
    """CommandTree.interaction_check 에 넣을 함수: tree.interaction_check = command_gate('music')."""
    async def _gate(interaction):
        return await check_command(interaction, bot)
    return _gate


async def check_component(interaction, default_level=None, default_what="") -> bool:
    """버튼·선택 입구. 표에 없는 버튼은 View 의 default_level 로. 막으면 본인에게만 안내하고 False."""
    custom_id = (interaction.data or {}).get("custom_id") or ""
    level, what = component_level(custom_id)
    if level is None:
        level, what = default_level, default_what
    if allowed(level, interaction.user):
        return True
    await _deny(interaction, deny_text(level, what or "이 버튼"))
    return False


class GuardedView(discord.ui.View):
    """버튼 View 의 공용 부모 — 콜백 전에 권한 표부터 확인.
    표에 없는 버튼(자동 custom_id)은 default_level 로 판정 — 공개 채널에 뜨는 1회용 화면은 여기에 타입을 적는다."""
    default_level = None
    default_what = "이 버튼"

    async def interaction_check(self, interaction) -> bool:
        return await check_component(interaction, self.default_level, self.default_what)


def table_text() -> str:
    """권한 표를 사람이 읽는 글로 (로그·문서용)."""
    lines = []
    for level in LEVELS:
        cmds = [f"/{n}" for bot, t in COMMANDS.items() for n in t if command_level(n, bot) == level]
        btns = sorted({what for cid, (_, what) in COMPONENTS.items() if component_level(cid)[0] == level})
        if cmds or btns:
            lines.append(f"[{level}] " + " · ".join(cmds + btns))
    return "\n".join(lines)
