# -*- coding: utf-8 -*-
"""RaidBook_perms (기능별 권한 표) 테스트 — 타입별 판정, '운영진' 역할만 운영진, 표 누락 검사."""
import asyncio
import os
import re
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import discord  # noqa: E402

import RaidBook_perms as rp  # noqa: E402

OWNER_ID = 1


class FakeMember(discord.Member):
    def __init__(self, uid, roles=(), admin=False, manage=False):  # noqa — discord.Member 생성자를 쓰지 않음
        self._fake_id = uid
        self._fake_roles = [SimpleNamespace(id=100 + i, name=n) for i, n in enumerate(roles)]
        self._fake_perms = discord.Permissions(administrator=admin, manage_guild=manage)
        self._fake_guild = SimpleNamespace(owner_id=OWNER_ID)

    @property
    def id(self):
        return self._fake_id

    @property
    def roles(self):
        return self._fake_roles

    @property
    def guild_permissions(self):
        return self._fake_perms

    @property
    def guild(self):
        return self._fake_guild


def _reset():
    rp.configure({})


def test_staff_is_exact_role_only():
    _reset()
    assert rp.is_staff(FakeMember(5, ["운영진"]))
    assert rp.is_staff(FakeMember(5, [" 운영진 "]))
    assert rp.is_staff(FakeMember(OWNER_ID))                                  # 서버 주인
    assert not rp.is_staff(FakeMember(5, ["정도운영진"]))                     # ○○운영진은 표시용
    assert not rp.is_staff(FakeMember(5, ["슈퍼맨운영진", "정도"]))
    assert not rp.is_staff(FakeMember(5, [], manage=True))                    # 서버 관리 권한만으론 아님
    assert not rp.is_staff("not a member")


def test_server_admin():
    _reset()
    assert rp.is_server_admin(FakeMember(5, [], manage=True))
    assert rp.is_server_admin(FakeMember(5, [], admin=True))
    assert rp.is_server_admin(FakeMember(OWNER_ID))
    assert not rp.is_server_admin(FakeMember(5, ["운영진"]))


def test_levels():
    _reset()
    member, staff = FakeMember(5, ["정도"]), FakeMember(6, ["운영진"])
    assert rp.allowed(rp.command_level("전투지원"), member)
    assert not rp.allowed(rp.command_level("보스알림"), member) and rp.allowed(rp.command_level("보스알림"), staff)
    assert not rp.allowed(rp.command_level("인증권한"), staff)
    assert rp.allowed(rp.command_level("재생"), member)                       # 음악봇 명령 → 표에 없음 → 통과
    assert not rp.allowed(rp.component_level("boss_loot")[0], member)        # 득템 기록 운영진 전용
    assert not rp.allowed(rp.component_level("boss_auction_create")[0], member)
    assert rp.allowed(rp.component_level("boss_board_bid")[0], member)       # 참석자 확인은 버튼 안에서
    assert rp.allowed(rp.component_level("combat_end")[0], member)           # 요청자 확인은 버튼 안에서
    assert rp.allowed(rp.component_level("rb_verify:open")[0], member)


def test_admin_role_config_and_overrides():
    rp.configure({"admin_role_names": ["운영진", "부운영"], "admin_role_ids": [777],
                  "permission_overrides": {"전투지원": "운영진", "boss_loot": "누구나", "x": "엉터리"}})
    assert rp.is_staff(FakeMember(5, ["부운영"]))
    m = FakeMember(5, ["정도"])
    m._fake_roles.append(SimpleNamespace(id=777, name="아무거나"))
    assert rp.is_staff(m)
    assert rp.command_level("전투지원") == rp.STAFF
    assert rp.component_level("boss_loot") == (rp.EVERYONE, "득템 기록")
    assert rp.command_level("x") is None
    _reset()


def test_check_command_denies_with_ephemeral():
    _reset()
    sent = []

    class Resp:
        def is_done(self):
            return False

        async def send_message(self, text, ephemeral=False):
            sent.append((text, ephemeral))

    def inter(name, user, itype=discord.InteractionType.application_command):
        return SimpleNamespace(data={"name": name}, user=user, type=itype, response=Resp())

    assert not asyncio.run(rp.check_command(inter("보스알림", FakeMember(5, ["정도운영진"]))))
    assert sent and sent[-1][1] is True and "운영진" in sent[-1][0]
    assert asyncio.run(rp.check_command(inter("보스알림", FakeMember(5, ["운영진"]))))
    n = len(sent)
    assert not asyncio.run(rp.check_command(inter("인원체크", FakeMember(5), discord.InteractionType.autocomplete)))
    assert len(sent) == n                                                      # 자동완성엔 안내 안 보냄


def _sources():
    out = {}
    for f in os.listdir(ROOT):
        if f.startswith("RaidBook_") and f.endswith(".py") and f not in ("RaidBook_Music_Bot.py", "RaidBook_TTS_Bot.py",
                                                                        "RaidBook_Watchdog.py", "RaidBook_Watchdog_GUI.py"):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                out[f] = fh.read()
    return out


def test_every_boss_command_is_in_table():
    src = _sources()["RaidBook_boss_Bot.py"]
    names = set(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', src))
    for m in re.finditer(r'@bot\.hybrid_command\((?:name="([^"]+)")?[^\n]*\n(?:@[^\n]*\n)*async def (\w+)', src):
        names.add(m.group(1) or m.group(2))
    missing = sorted(n for n in names if n not in rp.COMMANDS)
    assert not missing, f"권한 표에 없는 명령: {missing}"


def test_every_persistent_button_is_in_table():
    ids = set()
    for f, src in _sources().items():
        ids |= set(re.findall(r'custom_id="([a-z_:]+)"', src))
    consts = {"BTN_VOICECHECK": "combat_voicecheck", "BTN_ADD": "combat_attendee_add",
              "BTN_REMOVE": "combat_attendee_remove", "BTN_END": "combat_end"}
    ids |= set(consts.values())
    missing = sorted(i for i in ids if i not in rp.COMPONENTS)
    assert not missing, f"권한 표에 없는 버튼: {missing}"


def test_guarded_views():
    import RaidBook_boss_auction as ba
    import RaidBook_combat_support as cs
    assert issubclass(ba.BoardView, rp.GuardedView) and issubclass(ba.AuctionView, rp.GuardedView)
    assert issubclass(cs.CombatView, rp.GuardedView)
    assert "[운영진]" in rp.table_text() and "[누구나]" in rp.table_text()


def test_default_level_for_unlisted_buttons():
    _reset()

    class Resp:
        def is_done(self):
            return False

        async def send_message(self, text, ephemeral=False):
            pass

    def inter(user):
        return SimpleNamespace(data={"custom_id": "a1b2c3random"}, user=user, response=Resp())

    assert asyncio.run(rp.check_component(inter(FakeMember(5))))                       # 1회용 화면 기본 통과
    assert not asyncio.run(rp.check_component(inter(FakeMember(5)), rp.STAFF, "확인"))  # default_level 운영진
    assert asyncio.run(rp.check_component(inter(FakeMember(5, ["운영진"])), rp.STAFF, "확인"))


def test_every_persistent_view_is_guarded():
    """보스봇 add_view 로 등록하는 상시 View 는 모두 GuardedView 자식, /add·/remove 공개 확인 화면은 운영진."""
    srcs = _sources()
    boss = srcs["RaidBook_boss_Bot.py"]
    classes = {m.split(".")[-1] for m in re.findall(r"bot\.add_view\(([\w.]+)\(", boss)}
    assert classes, "add_view 를 못 찾음"
    allsrc = "\n".join(srcs.values())
    for c in classes:
        assert re.search(rf"class {c}\(rb_perms\.GuardedView\)", allsrc), f"{c} 가 GuardedView 가 아님"
    assert re.search(r"class _AttendeeEditView\(rb_perms\.GuardedView\):\n(?:.*\n){0,2}\s+default_level = rb_perms\.STAFF", boss)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
