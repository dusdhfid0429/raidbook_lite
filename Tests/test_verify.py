# -*- coding: utf-8 -*-
"""RaidBook_verify (서버 입장 인증) 테스트 — 설정 자리, 비밀번호 비교, 실패 횟수, 화면 문구, 제출 흐름."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import RaidBook_verify as rv  # noqa: E402


def test_ensure_section_once_and_keeps_edits():
    config = {}
    assert rv.ensure_section(config) is True
    assert config["verify"]["max_failures"] == 5 and config["verify"]["role_name"] == "인증됨"
    assert "verify_channel_id" in config
    assert rv.ensure_section(config) is False
    config["verify"]["password"] = "abc"
    del config["verify"]["fail_action"]
    assert rv.ensure_section(config) is True
    assert config["verify"]["password"] == "abc" and config["verify"]["fail_action"] == "kick"


def test_password_check():
    config = {}
    rv.ensure_section(config)
    assert not rv.Settings(config).check("")             # 비밀번호가 비면 항상 실패
    config["verify"]["password"] = " 정도1234 "
    s = rv.Settings(config)
    assert s.check("정도1234") and s.check("  정도1234\n")
    assert not s.check("정도123") and not s.check("정도12345") and not s.check(None)


def test_failure_store_persists():
    with tempfile.TemporaryDirectory() as d:
        st = rv.FailureStore(d)
        assert st.add_failure(1) == 1 and st.add_failure(1) == 2
        st.set_panel(10, 20)
        st2 = rv.FailureStore(d)
        assert st2.count(1) == 2 and st2.panel() == (10, 20)
        st2.reset(1)
        assert rv.FailureStore(d).count(1) == 0


def test_messages():
    config = {}
    rv.ensure_section(config)
    s = rv.Settings(config)
    desc = rv.panel_description(s)
    assert desc.startswith("1. 아래 버튼을 눌러 비밀번호를 입력하세요")
    assert "⚠️ 5회 틀리면 서버에서 내보내져요" in desc and desc.endswith("비밀번호는 운영진에게 문의하세요")
    assert rv.wrong_message(2, s) == "❌ 비밀번호가 틀렸어요. (남은 기회 3회)"
    assert rv.kicked_message(s) == "⛔ 5회 틀려서 서버에서 내보내졌어요."
    assert not rv.is_exhausted(4, s) and rv.is_exhausted(5, s)
    config["verify"]["max_failures"] = 0
    s0 = rv.Settings(config)
    assert "내보내져요" not in rv.panel_description(s0) and not rv.is_exhausted(99, s0)


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


class FakeMember(discord.Member):
    def __init__(self, uid, roles):  # noqa — discord.Member 생성자를 쓰지 않음
        self._uid, self._roles_list, self.kicked, self.added = uid, roles, False, []

    @property
    def id(self):
        return self._uid

    @property
    def roles(self):
        return self._roles_list

    @property
    def display_name(self):
        return f"user{self._uid}"

    async def add_roles(self, role, reason=None):
        self._roles_list.append(role)
        self.added.append(role)

    async def kick(self, reason=None):
        self.kicked = True


def _run_flow(answers, max_failures=3):
    role = SimpleNamespace(id=55, name="인증됨")
    guild = SimpleNamespace(get_role=lambda rid: role if rid == 55 else None, roles=[role])
    config = {}
    rv.ensure_section(config)
    config["verify"].update(password="pw", role_id=55, max_failures=max_failures)
    with tempfile.TemporaryDirectory() as d:
        bot = SimpleNamespace(get_channel=lambda cid: None)
        v = rv.Verifier(bot, config, d, save_config=lambda: None)
        member = FakeMember(7, [])
        replies = []
        for a in answers:
            inter = SimpleNamespace(user=member, guild=guild, response=FakeResponse())
            asyncio.run(v.submit(inter, a))
            replies += inter.response.sent
        return member, replies, v


def test_submit_success_after_failures():
    member, replies, v = _run_flow(["x", "pw"])
    assert all(eph for _, eph in replies)                      # 모두 본인에게만
    assert replies[0][0] == "❌ 비밀번호가 틀렸어요. (남은 기회 2회)"
    assert replies[1][0] == rv.MSG_OK and member.added and not member.kicked
    assert v.store.count(7) == 0


def test_submit_kick_at_limit():
    member, replies, v = _run_flow(["a", "b", "c"])
    assert replies[-1][0] == "⛔ 3회 틀려서 서버에서 내보내졌어요." and member.kicked
    assert not member.added and v.store.count(7) == 0          # 다시 들어오면 처음부터


def test_already_verified():
    member, replies, _ = _run_flow(["pw", "pw"])
    assert replies[1][0] == rv.MSG_ALREADY


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL OK")
