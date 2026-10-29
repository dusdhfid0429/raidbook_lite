# -*- coding: utf-8 -*-
"""
RaidBook_schedule.py
공용 알림 스케줄 엔진 — Boss Bot / Combat Bot 공용

Discord 의존성 없음. 타이밍(언제 알림을 낼지)만 담당하고,
실제 메시지 게시는 각 봇이 넘긴 async 콜백 post_cb(name, event_dt)가 수행한다.
콜백 안에서 각 봇이 자기 Excel/JSON/채널 기준으로 동작하므로 데이터는 봇별로 분리 유지.

지원 유형 (보스봇 기존 동작과 동일 의미):
- interval: 기준 시각(start_hour:start_minute)부터 interval_hours 간격 반복,
            이벤트 alert_minutes 전에 콜백 호출. 런타임 주기 변경 반영.
- daily:    매일 고정 시각 목록(times, "24:00"=다음날 00:00 지원),
            이벤트 alert_minutes 전에 콜백 호출. 루프 상단 재계산으로 경계 누락 방지.

상태 딕셔너리는 호출자가 소유한다 (기존 boss_states / boss_daily_states 그대로 사용 가능):
- interval_states: {name: {"task": Task, "interval_hours": N}}
- daily_states:    {name: {time_str: Task}}
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

_default_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# 공용 스케줄 레지스트리 — 여러 봇이 "지금 실제로 도는 스케줄"을 파일로 공유.
# 겹침 판정(예: 보스 알림과 같은 시각의 전투 알림 생략)이 config가 아닌
# 실제 실행 상태를 참조할 수 있게 한다. 구조:
#   {"boss": {"카스파": {"type": "interval", "interval_hours": 2, ...}},
#    "combat": {"드레이크": {"type": "interval", ...}}}
# ════════════════════════════════════════════════════════════════

def load_registry(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_registry(path, data, logger=None):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        (logger or _default_log).info(f"[스케줄 레지스트리 저장 실패] {e}")


def publish_schedule(path, bot_key, name, spec, logger=None):
    """실행 시작한 스케줄을 레지스트리에 게시."""
    data = load_registry(path)
    data.setdefault(bot_key, {})[str(name)] = dict(spec)
    _write_registry(path, data, logger)


def remove_schedule_entry(path, bot_key, name, logger=None):
    """중지된 스케줄을 레지스트리에서 제거."""
    data = load_registry(path)
    if str(name) in (data.get(bot_key) or {}):
        data[bot_key].pop(str(name))
        _write_registry(path, data, logger)


def clear_bot_schedules(path, bot_key, logger=None):
    """봇 시작 시 자기 항목 초기화 (재시작 후 유령 스케줄 방지)."""
    data = load_registry(path)
    if data.get(bot_key):
        data[bot_key] = {}
        _write_registry(path, data, logger)


def schedule_conflict_at(path, bot_key, spawn_dt, *, tolerance_min=0.5):
    """레지스트리의 bot_key 스케줄 중 spawn_dt 시각에 이벤트가 예정된 항목 이름 반환. 없으면 None."""
    entries = load_registry(path).get(bot_key)
    if not isinstance(entries, dict):
        return None
    hhmm = spawn_dt.strftime("%H:%M")
    for name, spec in entries.items():
        if not isinstance(spec, dict):
            continue
        stype = str(spec.get("type", "")).lower()
        if stype == "interval":
            try:
                iv = float(spec.get("interval_hours") or 0)
                sh = int(spec.get("start_hour", 0) or 0)
                sm = int(spec.get("start_minute", 0) or 0)
            except (TypeError, ValueError):
                continue
            if iv <= 0:
                continue
            base = spawn_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
            diff_min = (spawn_dt - base).total_seconds() / 60.0
            step = iv * 60.0
            if diff_min >= 0:
                rem = diff_min % step
                if rem < tolerance_min or step - rem < tolerance_min:
                    return str(name)
        elif stype == "daily":
            times = [("00:00" if t == "24:00" else str(t)) for t in (spec.get("times") or [])]
            if hhmm in times:
                return str(name)
    return None


def compute_first_interval_alert(start_hour, start_minute, interval_hours, alert_minutes, now=None):
    """interval형의 첫 알림 시각 계산. 기준 시각에서 주기를 더해가며 now 이후 첫 알림을 찾는다."""
    now = now or datetime.now()
    first_event = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    first_alert = first_event - timedelta(minutes=alert_minutes)
    while first_alert < now:
        first_event += timedelta(hours=interval_hours)
        first_alert = first_event - timedelta(minutes=alert_minutes)
    return first_alert


async def run_interval(name, alert_dt, interval_hours, alert_minutes, post_cb,
                       states, log=None, reschedule=True):
    """interval형 1회 실행 + 자기 재예약 (기존 schedule_boss 루프 의미 유지).

    alert_dt 까지 대기 → post_cb(name, event_dt) → 이벤트 시각까지 대기 →
    states[name]["interval_hours"] (런타임 변경 반영) 기준으로 다음 태스크 재예약.
    """
    log = log or _default_log
    now = datetime.now()
    delay = (alert_dt - now).total_seconds()
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

    event_dt = alert_dt + timedelta(minutes=alert_minutes)
    try:
        await post_cb(name, event_dt)
    except Exception as e:
        log.info(f"[스케줄 알림 실패] {name} {event_dt.strftime('%H:%M')}: {e}")

    if not reschedule:
        return
    # 런타임에 변경된 주기 반영
    current_interval = states[name]["interval_hours"] if name in states else interval_hours
    next_alert = event_dt + timedelta(hours=current_interval) - timedelta(minutes=alert_minutes)
    delay2 = (event_dt - datetime.now()).total_seconds()
    if delay2 > 0:
        try:
            await asyncio.sleep(delay2)
        except asyncio.CancelledError:
            return

    log.info(f"[다음 스케줄] {name} | 다음 알림: {next_alert.strftime('%H:%M')} | 주기: {current_interval}시간")
    task = asyncio.ensure_future(run_interval(
        name, next_alert, current_interval, alert_minutes, post_cb, states, log))
    if name in states:
        states[name]["task"] = task


def start_interval(name, interval_hours, start_hour, start_minute, alert_minutes,
                   post_cb, states, log=None):
    """interval형 스케줄 시작. 기존 태스크가 있으면 취소 후 교체. 반환: Task"""
    log = log or _default_log
    old = states.get(name, {}).get("task")
    if old and not old.done():
        old.cancel()
    first_alert = compute_first_interval_alert(start_hour, start_minute, interval_hours, alert_minutes)
    log.info(f"[{name}] 첫 알림: {first_alert.strftime('%m-%d %H:%M')} | 주기 {interval_hours}시간")
    task = asyncio.ensure_future(run_interval(
        name, first_alert, interval_hours, alert_minutes, post_cb, states, log))
    states[name] = {"task": task, "interval_hours": interval_hours}
    return task


async def run_daily(name, spawn_time_str, alert_minutes, post_cb, daily_states, log=None, already_sent=None):
    """daily형 무한 루프 (기존 schedule_boss_fixed_time 의미 유지).

    매 반복 상단에서 다음 occurrence를 재계산 — 알림 직후 재슬립으로 인한
    경계 누락(정확히 그 시각에 깨어 '이미 지남' 처리) 버그를 피한다.
    daily_states에서 name이 빠지면 루프 종료.
    시작 시점이 알림 시각과 출현 시각 사이면(알림 직후 재시작 등) 그 회차를 바로 보낸다.
    already_sent(name, spawn_dt, alert_dt) → True면 이미 보낸 회차라 건너뛴다 (중복 알림 방지).
    """
    log = log or _default_log
    try:
        if spawn_time_str == "24:00":
            actual_h, actual_m, next_day_spawn = 0, 0, True
        else:
            actual_h, actual_m = map(int, str(spawn_time_str).split(":"))
            if not (0 <= actual_h <= 23 and 0 <= actual_m <= 59):
                raise ValueError(spawn_time_str)
            next_day_spawn = False
    except Exception:
        log.warning(f"[daily 스케줄 중단] {name} | 시각 형식 오류: {spawn_time_str!r} (HH:MM 필요)")
        return

    first = True
    while True:
        if name not in daily_states:
            break

        now = datetime.now()
        spawn_dt = now.replace(hour=actual_h, minute=actual_m, second=0, microsecond=0)
        if next_day_spawn:
            spawn_dt += timedelta(days=1)
        alert_dt = spawn_dt - timedelta(minutes=alert_minutes)

        # 알림 시각~출현 시각 사이에 (재)시작됐으면 이번 회차를 놓치지 않고 바로 보낸다
        if first:
            first = False
            for back in (0, 1):   # 자정을 넘는 회차(23:40 알림 → 00:00 출현) 대비 하루 전 회차도 확인
                s_dt, a_dt = spawn_dt - timedelta(days=back), alert_dt - timedelta(days=back)
                if a_dt <= now < s_dt:
                    sent = False
                    if already_sent is not None:
                        try:
                            sent = bool(already_sent(name, s_dt, a_dt))
                        except Exception as e:
                            log.info(f"[daily] {name} 중복 확인 실패 — 보내지 않음: {e}")
                            sent = True
                    if not sent:
                        log.info(f"[daily] {name} {spawn_time_str} | 알림 시각이 지나 시작됨 → 바로 알림 (출현 {s_dt.strftime('%m-%d %H:%M')})")
                        try:
                            await post_cb(name, s_dt)
                        except Exception as e:
                            log.info(f"[daily 알림 실패] {name} {spawn_time_str}: {e}")
                    break

        # 이미 지난 경우 다음날로 (복수 일 밀릴 수 있으므로 while)
        while alert_dt <= now:
            spawn_dt += timedelta(days=1)
            alert_dt += timedelta(days=1)

        log.info(f"[daily] {name} {spawn_time_str} | 다음 알림: {alert_dt.strftime('%m-%d %H:%M')}")

        delay = (alert_dt - datetime.now()).total_seconds()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        if name not in daily_states:
            break

        try:
            await post_cb(name, spawn_dt)
        except Exception as e:
            log.info(f"[daily 알림 실패] {name} {spawn_time_str}: {e}")
        # 루프 상단에서 다음 occurrence 재계산 (방금 시각은 while 가드가 넘긴다)


def start_daily(name, times, alert_minutes, post_cb, daily_states, log=None):
    """daily형 스케줄 시작 (시각 목록). 기존 태스크가 있으면 취소 후 교체. 반환: {time: Task}"""
    log = log or _default_log
    if name in daily_states:
        for task in daily_states[name].values():
            if not task.done():
                task.cancel()
    daily_states[name] = {}
    for t in times:
        task = asyncio.ensure_future(run_daily(name, t, alert_minutes, post_cb, daily_states, log))
        daily_states[name][t] = task
    log.info(f"[daily] {name} 시작: {times} | {alert_minutes}분 전 알림")
    return daily_states[name]


def stop_schedule(name, states=None, daily_states=None):
    """이름의 스케줄 중지 (interval/daily 모두). 반환: 중지된 항목 수"""
    stopped = 0
    if states is not None and name in states:
        task = states[name].get("task")
        if task and not task.done():
            task.cancel()
        del states[name]
        stopped += 1
    if daily_states is not None and name in daily_states:
        for task in daily_states[name].values():
            if not task.done():
                task.cancel()
        del daily_states[name]
        stopped += 1
    return stopped
