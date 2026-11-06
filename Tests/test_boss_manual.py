# -*- coding: utf-8 -*-
"""RaidBook_boss_manual (/보스알림) 테스트 — 젠 일시 해석·범위, 보스 목록, 중복 확인, 팝업 제출."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RaidBook_boss_manual as bm  # noqa: E402

NOW = datetime(2026, 11, 7, 15, 0)


def test_parse_12_digits_only():
    assert bm.parse_spawn_datetime("202611071700") == datetime(2026, 11, 7, 17, 0)
    assert bm.parse_spawn_datetime(" 2026-11-07 17:00 ") == datetime(2026, 11, 7, 17, 0)
    assert bm.parse_spawn_datetime("1700") is None
    assert bm.parse_spawn_datetime("2026110717000") is None
    assert bm.parse_spawn_datetime("202613071700") is None      # 13월
    assert bm.parse_spawn_datetime("202611072500") is None      # 25시
    assert bm.parse_spawn_datetime("") is None and bm.parse_spawn_datetime(None) is None


def test_range_2h_ahead_24h_past():
    ok = lambda t: bm.check_spawn_datetime(t, NOW)[0] is not None  # noqa: E731
    assert ok("202611071700")            # 정확히 2시간 남음 → 허용
    assert not ok("202611071701")        # 2시간 넘게 남음 → 잘못 입력
    assert ok("202611061500")            # 정확히 24시간 지남 → 허용 (즉시 마감용)
    assert not ok("202611061459")        # 24시간 넘게 지남 → 잘못 입력
    assert ok("202611071500")
    dt, err = bm.check_spawn_datetime("202611071800", NOW)
    assert dt is None and "2시간" in err
    dt, err = bm.check_spawn_datetime("202611051500", NOW)
    assert dt is None and "24시간" in err
    dt, err = bm.check_spawn_datetime("17:00", NOW)
    assert dt is None and "12자리" in err


def test_fmt_spawn_weekday():
    assert bm.fmt_spawn(datetime(2026, 11, 7, 17, 0)) == "11/07(토) 17:00"


def test_boss_options_config_first_then_images_dedup():
    with tempfile.TemporaryDirectory() as d:
        for f in ("카스파.png", "드레이크.png", "readme.txt", "커츠.jpg"):
            open(os.path.join(d, f), "w").close()
        opts = bm.boss_options({"네크로맨서": {}}, {"드레이크": {}}, [d, os.path.join(d, "없는폴더")])
        assert opts == ["네크로맨서", "드레이크", "카스파", "커츠"]
    many = {f"보스{i}": {} for i in range(40)}
    assert len(bm.boss_options(many, {})) == 25


def test_find_duplicate_same_boss_time_and_date():
    active = {"111": {"boss_name": "카스파", "boss_time": "17:00", "created_date": "2026-11-07"},
              "222": {"boss_name": "카스파", "boss_time": "17:00", "created_date": "2026-11-06"}}
    assert bm.find_duplicate(active, "카스파", datetime(2026, 11, 7, 17, 0)) == "111"
    assert bm.find_duplicate(active, "카스파", datetime(2026, 11, 8, 17, 0)) is None
    assert bm.find_duplicate(active, "커츠", datetime(2026, 11, 7, 17, 0)) is None


def test_resolve_name_typed_wins():
    assert bm.resolve_boss_name("  카스파  ", "드레이크") == "카스파"
    assert bm.resolve_boss_name("", "드레이크") == "드레이크"
    assert bm.resolve_boss_name(" ", "") == ""


class _Resp:
    def __init__(self):
        self.sent = []
        self.deferred = False

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True


def _submit(modal, *, pick=None, name="", when=""):
    if modal.boss_select is not None:
        modal.boss_select._values = [pick] if pick else []
    modal.name_input._value = name
    modal.when_input._value = when
    inter = SimpleNamespace(response=_Resp(), followup=None)
    asyncio.run(modal.on_submit(inter))
    return inter


def test_modal_submit_calls_create_with_name_and_time():
    created = []

    async def on_create(inter, name, dt):
        created.append((name, dt))

    async def run():
        return bm.BossAlertModal(["드레이크", "카스파"], on_create)

    modal = asyncio.run(run())
    now = datetime.now().replace(second=0, microsecond=0)
    when = now.strftime("%Y%m%d%H%M")
    inter = _submit(modal, pick="드레이크", when=when)
    assert inter.response.deferred and created == [("드레이크", now)]
    inter = _submit(modal, pick="드레이크", name="카스파", when=when)
    assert created[-1][0] == "카스파"
    inter = _submit(modal, when=when)
    assert "보스를 고르거나" in inter.response.sent[0] and len(created) == 2
    inter = _submit(modal, pick="드레이크", when="1700")
    assert "12자리" in inter.response.sent[0] and len(created) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
