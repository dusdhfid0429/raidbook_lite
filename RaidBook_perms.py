# -*- coding: utf-8 -*-
"""🛡️ 기능별 권한 — 명령·버튼마다 권한 타입을 표(데이터)로 두고, 들어오면 이 표부터 확인한다.

권한 타입
  누구나       아무나
  운영진       '운영진' 역할(admin_role_names·admin_role_ids) 또는 서버 주인
               (정도운영진·슈퍼맨운영진 같은 ○○운영진 역할은 표시용이라 아님)
  서버관리자   서버 주인 또는 '관리자'·'서버 관리' 권한
  운영진·요청자 운영진은 바로 통과, 아니면 버튼 안에서 요청자 본인인지 확인 (전투종료)
  참석자       누구나 들어오고, 버튼 안에서 그 보스 참석자인지 확인 (입찰)

입구는 두 곳뿐:
  - 슬래시 명령: 보스봇 CommandTree.interaction_check → check_command
  - 버튼·선택: 상시 버튼 View 의 공용 부모 GuardedView.interaction_check → check_component
표에 없는 명령은 이 프로세스 명령이 아니라고 보고 통과(같은 토큰을 쓰는 음악봇 명령), 표에 없는 버튼은
상시 버튼을 누른 뒤 뜨는 1회용 화면이라 통과한다.
boss_config.json 의 permission_overrides 로 타입을 바꿀 수 있다 (예: {"전투지원": "운영진"}).
"""
import discord

EVERYONE = "누구나"
STAFF = "운영진"
SERVER_ADMIN = "서버관리자"
STAFF_OR_REQUESTER = "운영진·요청자"
ATTENDEE = "참석자"
LEVELS = (EVERYONE, STAFF, SERVER_ADMIN, STAFF_OR_REQUESTER, ATTENDEE)

STAFF_ROLE_NAME = "운영진"

# ── 슬래시 명령 (이름 → 타입) ─────────────────────────────────────────────
COMMANDS = {
    # 누구나
    "전투지원": EVERYONE,
    # 운영진
    "보스알림": STAFF, "전파": STAFF, "인원체크": STAFF, "인원현황": STAFF,
    "startboss": STAFF, "stopboss": STAFF, "setboss": STAFF, "listboss": STAFF,
    "startdailyboss": STAFF, "stopdailyboss": STAFF, "listdailyboss": STAFF, "setrefresh": STAFF,
    "create": STAFF, "test": STAFF, "close": STAFF, "activate": STAFF, "extend": STAFF,
    "reopen": STAFF, "delete": STAFF, "recover": STAFF, "sync": STAFF,
    "add": STAFF, "remove": STAFF, "rename": STAFF, "fixid": STAFF, "fixnames": STAFF, "fixmsg": STAFF,
    "attachloot": STAFF, "history": STAFF, "stats": STAFF, "refresh": STAFF, "save": STAFF,
    "restart": STAFF, "collectmembers": STAFF,
    # 서버관리자
    "인증권한": SERVER_ADMIN,
}

# ── 상시 버튼·선택 (custom_id → (타입, 이름)) ──────────────────────────────
COMPONENTS = {
    # 보스알림
    "boss_voicecheck": (STAFF, "인원체크"),
    "boss_attendee_add": (STAFF, "인원 추가"),
    "boss_attendee_remove": (STAFF, "인원 제거"),
    "boss_loot": (STAFF, "득템 기록"),
    "boss_auction_create": (STAFF, "경매생성"),
    "boss_party": (EVERYONE, "파티 확인"),
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
    "boss_pending_refresh": (EVERYONE, "새로고침"),
    # 전투지원
    "combat_voicecheck": (STAFF, "인원체크"),
    "combat_attendee_add": (STAFF, "인원 추가"),
    "combat_attendee_remove": (STAFF, "인원 제거"),
    "combat_end": (STAFF_OR_REQUESTER, "전투종료"),
    # 🔐 #인증
    "rb_verify:open": (EVERYONE, "비밀번호 입력"),
}

_admin_role_names = {STAFF_ROLE_NAME}
_admin_role_ids = set()
_overrides = {}


def configure(config):
    """보스봇 시작 때 한 번 — admin_role_names·admin_role_ids·permission_overrides 를 읽는다."""
    global _admin_role_names, _admin_role_ids, _overrides
    names = config.get("admin_role_names", [STAFF_ROLE_NAME]) or []
    _admin_role_names = {str(n).strip() for n in names if str(n).strip()}
    ids = set()
    for r in config.get("admin_role_ids") or []:
        try:
            ids.add(int(r))
        except (TypeError, ValueError):
            pass
    _admin_role_ids = ids
    raw = config.get("permission_overrides") or {}
    _overrides = {str(k): str(v) for k, v in raw.items()
                  if isinstance(raw, dict) and str(v) in LEVELS and not str(k).startswith("_")}


def _is_owner(member) -> bool:
    guild = getattr(member, "guild", None)
    return guild is not None and getattr(member, "id", None) == guild.owner_id


def is_staff(member) -> bool:
    """운영진: 서버 주인 또는 '운영진' 역할(admin_role_names·admin_role_ids)."""
    if not isinstance(member, discord.Member):
        return False
    if _is_owner(member):
        return True
    return any(r.id in _admin_role_ids or str(r.name).strip() in _admin_role_names for r in member.roles)


def is_server_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if _is_owner(member):
        return True
    p = member.guild_permissions
    return bool(p.administrator or p.manage_guild)


def command_level(name):
    """명령 타입. 표에 없으면 None (이 프로세스 명령 아님)."""
    if name in _overrides:
        return _overrides[name]
    return COMMANDS.get(name)


def component_level(custom_id):
    """버튼 (타입, 이름). 표에 없으면 (None, '')."""
    if custom_id in _overrides:
        return _overrides[custom_id], COMPONENTS.get(custom_id, (None, custom_id))[1]
    return COMPONENTS.get(custom_id, (None, ""))


def allowed(level, member) -> bool:
    """이 타입을 이 사람이 바로 쓸 수 있는지. 운영진·요청자/참석자는 입구에서 통과시키고 버튼 안에서 마저 확인."""
    if level is None or level in (EVERYONE, STAFF_OR_REQUESTER, ATTENDEE):
        return True
    if level == STAFF:
        return is_staff(member)
    if level == SERVER_ADMIN:
        return is_server_admin(member)
    return False


def deny_text(level, what) -> str:
    who = "서버 관리 권한자" if level == SERVER_ADMIN else "운영진"
    return f"⛔ {what}은(는) {who}만 쓸 수 있어요."


async def _deny(interaction, text):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def check_command(interaction) -> bool:
    """슬래시 명령 입구. 막으면 본인에게만 안내하고 False."""
    data = interaction.data or {}
    name = data.get("name") or ""
    level = command_level(name)
    if allowed(level, interaction.user):
        return True
    if interaction.type == discord.InteractionType.application_command:
        await _deny(interaction, deny_text(level, f"/{name}"))
    return False


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
        cmds = [f"/{n}" for n in COMMANDS if command_level(n) == level]
        btns = sorted({what for cid, (_, what) in COMPONENTS.items() if component_level(cid)[0] == level})
        if cmds or btns:
            lines.append(f"[{level}] " + " · ".join(cmds + btns))
    return "\n".join(lines)
