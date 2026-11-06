# -*- coding: utf-8 -*-
"""RaidBook_verify_perms (/인증권한) 테스트 — 채널별 새 권한 계산, 미리보기, 바뀌는 채널만 고르기."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import RaidBook_verify_perms as vp  # noqa: E402

PO = discord.PermissionOverwrite


class FakeRole(discord.Role):
    def __init__(self, rid, name, managed=False):  # noqa — discord.Role 생성자를 쓰지 않음
        self.id, self.name, self._fake_managed = rid, name, managed

    @property
    def managed(self):
        return self._fake_managed


class FakeMember(discord.Member):
    def __init__(self, uid):  # noqa
        self._uid = uid

    @property
    def id(self):
        return self._uid

    def __hash__(self):
        return hash(self._uid)

    def __eq__(self, other):
        return isinstance(other, FakeMember) and other._uid == self._uid


EVERYONE = FakeRole(1, "@everyone")
VERIFIED = FakeRole(2, "인증됨")
JD = FakeRole(3, "정도")
STAFF = FakeRole(4, "운영진")
CLAN_STAFF = FakeRole(6, "정도운영진")   # 표시용 — 권한 남기지 않음 (2026-11-06)
BOTROLE = FakeRole(5, "BanaRing", managed=True)
USER = FakeMember(99)


def ch(cid, overwrites, name="ch"):
    return SimpleNamespace(id=cid, name=name, overwrites=overwrites)


def test_public_channel():
    c = ch(10, {EVERYONE: PO(send_messages=True), JD: PO(view_channel=True), USER: PO(connect=True)})
    new, removed, private = vp.plan_channel(c, EVERYONE, VERIFIED, "운영진", 77)
    assert not private and removed == ["정도"]
    assert new[EVERYONE].view_channel is False and new[EVERYONE].send_messages is True   # 다른 칸 유지
    assert new[VERIFIED].view_channel is True
    assert JD not in new and new[USER].connect is True                                  # 멤버 개별 권한 유지


def test_private_channel_keeps_staff_and_bots():
    c = ch(11, {EVERYONE: PO(view_channel=False), STAFF: PO(view_channel=True),
                BOTROLE: PO(view_channel=True), JD: PO(view_channel=True), CLAN_STAFF: PO(view_channel=True)})
    new, removed, private = vp.plan_channel(c, EVERYONE, VERIFIED, "운영진", 77)
    assert private and removed == ["정도", "정도운영진"] and CLAN_STAFF not in new
    assert VERIFIED not in new                       # 비공개 채널은 인증됨에게 열지 않음
    assert new[STAFF].view_channel is True and BOTROLE in new


def test_keep_suffix_empty_removes_staff_too():
    c = ch(12, {STAFF: PO(view_channel=True)})
    new, removed, _ = vp.plan_channel(c, EVERYONE, VERIFIED, "", 77)
    assert removed == ["운영진"] and STAFF not in new


def test_verify_channel():
    c = ch(77, {})
    new, _, _ = vp.plan_channel(c, EVERYONE, VERIFIED, "운영진", 77)
    assert new[EVERYONE].view_channel is True and new[EVERYONE].send_messages is False
    assert new[VERIFIED].view_channel is False


def test_build_plan_skips_already_done_and_preview():
    done, _, _ = vp.plan_channel(ch(20, {JD: PO(view_channel=True)}), EVERYONE, VERIFIED, "운영진", 77)
    channels = [ch(20, done, "done"), ch(21, {JD: PO(view_channel=True)}, "todo")]
    guild = SimpleNamespace(categories=[], channels=channels, default_role=EVERYONE)
    plan = vp.build_plan(guild, VERIFIED, "운영진", 77)
    assert [c.name for c, *_ in plan] == ["todo"]
    text = vp.preview_text(plan, VERIFIED, "운영진", 77)
    assert "채널 1개" in text and "정도(1)" in text and "/인증권한 복구" in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL OK")
