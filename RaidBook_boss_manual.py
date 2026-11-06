# -*- coding: utf-8 -*-
"""📢 /보스알림 — 운영진이 팝업으로 보스알림을 직접 만든다.

팝업: 보스 선택(목록) + 보스 이름(목록에 없을 때만) + 젠 일시(YYYYMMDDHHMM 12자리).
게시·버튼·인원체크·마감·저장은 보스봇의 자동 알림 함수(_post_boss_alert)를 그대로 쓴다 → 자동 알림과 동일.
이 모듈은 순수 로직(일시 해석·범위 확인·보스 목록·중복 확인)과 팝업만 담당.
"""
import os
import re
from datetime import datetime, timedelta

import discord
from discord import ui

MAX_AHEAD = timedelta(hours=2)     # 지금보다 2시간 넘게 남은 일시 → 잘못 입력
MAX_PAST = timedelta(hours=24)     # 24시간 넘게 지난 일시 → 잘못 입력
MAX_OPTIONS = 25                   # 디스코드 선택 메뉴 최대 항목 수
NAME_MAX = 20
WEEKDAYS = "월화수목금토일"
DATETIME_HINT = "젠 일시는 년월일시분 12자리로 적어 주세요. 예) 202611071700 = 2026년 11월 07일 17:00"


def parse_spawn_datetime(text):
    """'202611071700' → datetime. 기호·띄어쓰기는 무시하고 숫자 12자리만 읽는다. 틀리면 None."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 12:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d%H%M")
    except ValueError:
        return None


def check_spawn_datetime(text, now=None):
    """(일시, 오류 문장). 형식이 틀리거나 2시간 넘게 남았거나 24시간 넘게 지났으면 일시=None."""
    now = now or datetime.now()
    dt = parse_spawn_datetime(text)
    if dt is None:
        return None, f"❌ {DATETIME_HINT}"
    if dt - now > MAX_AHEAD:
        return None, (f"❌ {fmt_spawn(dt)}은(는) 지금부터 2시간 넘게 남았어요. "
                      "잘못 입력한 것 같아요. 일시를 확인해 주세요.")
    if now - dt > MAX_PAST:
        return None, (f"❌ {fmt_spawn(dt)}은(는) 24시간 넘게 지났어요. "
                      "잘못 입력한 것 같아요. 일시를 확인해 주세요.")
    return dt, None


def fmt_spawn(dt):
    """2026-11-07 17:00 → '11/07(토) 17:00'"""
    return f"{dt:%m/%d}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def clean_boss_name(text):
    return re.sub(r"\s+", " ", (text or "").strip())[:NAME_MAX]


def boss_options(bosses_cfg, daily_cfg, image_dirs=()):
    """팝업 보스 목록: 설정의 보스(bosses·daily_bosses) 먼저, 그다음 보스 이미지 파일 이름. 중복 없이 최대 25개."""
    names = []
    for name in list(bosses_cfg or {}) + list(daily_cfg or {}):
        name = clean_boss_name(str(name))
        if name and name not in names:
            names.append(name)
    for d in image_dirs:
        try:
            files = sorted(os.listdir(d))
        except OSError:
            continue
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                continue
            stem = clean_boss_name(stem)
            if stem and stem not in names:
                names.append(stem)
    return names[:MAX_OPTIONS]


def find_duplicate(active_messages, boss_name, spawn_dt):
    """진행 중(활성) 알림 중 같은 보스·같은 젠 일시가 있으면 그 메시지 ID, 없으면 None."""
    boss_time = spawn_dt.strftime("%H:%M")
    date = spawn_dt.strftime("%Y-%m-%d")
    for mid, info in (active_messages or {}).items():
        if (info.get("boss_name") == boss_name and info.get("boss_time") == boss_time
                and (info.get("created_date") or date) == date):
            return str(mid)
    return None


def resolve_boss_name(typed, picked):
    """직접 입력이 있으면 그 이름, 없으면 목록에서 고른 이름."""
    return clean_boss_name(typed) or clean_boss_name(picked)


class BossAlertModal(ui.Modal):
    """보스 선택 + 보스 이름(목록에 없을 때만) + 젠 일시 → on_create(interaction, boss_name, spawn_dt).
    on_create 는 interaction.response.defer 이후에 불리고, 결과 안내는 직접 followup 으로 보낸다."""

    def __init__(self, options, on_create):
        super().__init__(title="📢 보스알림 생성", timeout=300)
        self.on_create = on_create
        self.boss_select = None
        if options:
            self.boss_select = ui.Select(
                placeholder="보스를 고르세요", required=False, min_values=0, max_values=1,
                options=[discord.SelectOption(label=n[:100], value=n[:100]) for n in options[:MAX_OPTIONS]])
            self.add_item(ui.Label(text="보스 선택", component=self.boss_select))
        self.name_input = ui.TextInput(placeholder="예) 카스파", required=not options,
                                       max_length=NAME_MAX)
        self.add_item(ui.Label(text="보스 이름" + (" (목록에 없을 때만)" if options else ""),
                               component=self.name_input))
        self.when_input = ui.TextInput(placeholder="예) 202611071700", required=True,
                                       min_length=12, max_length=20)
        self.add_item(ui.Label(text="젠 일시 (년월일시분 12자리)",
                               description="202611071700 = 2026년 11월 07일 17:00",
                               component=self.when_input))

    async def on_submit(self, interaction: discord.Interaction):
        picked = self.boss_select.values[0] if self.boss_select and self.boss_select.values else ""
        boss_name = resolve_boss_name(self.name_input.value, picked)
        if not boss_name:
            await interaction.response.send_message("❌ 보스를 고르거나 이름을 적어 주세요.", ephemeral=True)
            return
        spawn_dt, err = check_spawn_datetime(self.when_input.value)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.on_create(interaction, boss_name, spawn_dt)
        except Exception as e:
            await interaction.followup.send(f"❌ 보스알림 생성 실패: {type(e).__name__}: {e}", ephemeral=True)
