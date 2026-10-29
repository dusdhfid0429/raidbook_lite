# -*- coding: utf-8 -*-
"""
RaidBook_boss_auction.py
보스봇 전리품 경매 (카운트다운 방식, 포인트 연동 없음).

흐름 (단일 경매 — 진행판은 파일 아래쪽 "경매 진행판" 참고)
  1) 전리품 공지의 🔨 경매시작 → 세팅 입력(물품·카운트 초·시작가·호가) → 완료
     → 경매 채널에 '대기' 상태 경매 메시지 게시
  2) 경매 메시지의 ▶️ 경매시작 → 진행 상태 전환
  3) 🙋 입찰 → 첫 입찰은 시작가, 이후 현재가 + 호가. 최종 입찰자·금액 메시지 갱신
     입찰은 전리품을 획득한 보스 알림의 참석자만 가능 (source_message_id → get_attendees).
  4) 시작(또는 마지막 입찰) 후 카운트(초) 동안 새 입찰이 없으면 입찰 버튼 비활성 + 경매 마감.
     입찰자가 있으면 낙찰, 없으면 유찰. 관리자는 ⏹️ 경매종료로 즉시 마감 가능.

상태 저장: Data/boss_auction_state.json {경매 메시지ID: rec} (write_json_atomic, 메모리 캐시).

시간 기준: 입찰·시작 시각은 봇 처리 시각이 아니라 사용자가 버튼을 누른 순간
(interaction.created_at = Discord 서버 시각)을 쓴다. 마감 판정도 같은 Discord 시각 기준이며,
PC 시계와 Discord 시계의 차이(clock_offset)를 보정하고, 마감 직전 클릭이 처리 지연으로
누락되지 않도록 CLOSE_GRACE_SEC 만큼 기다린 뒤 마감한다.
재시작 시 setup()의 restore()로 진행 중 경매의 마감 타이머를 다시 건다.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime

import discord
from discord import ui

from RaidBook_common import write_json_atomic

CLOSE_GRACE_SEC = 0.8   # 마감 직전 클릭의 네트워크·처리 지연 허용 (판정은 클릭 시각 기준)
REFRESH_DELAY_SEC = 0.3   # 잠금이 없을 때 최종 상태로 다시 그리기까지 최소 대기

AMOUNT_HINT = "숫자 또는 단위 표기 (예: 80만, 1.5억, 120만5천, 3,750,000)"


def parse_amount_kr(text):
    """"80만"·"1.5억"·"120만5천"·"3,750,000" → int. 해석 불가 시 None. (회계봇 _parse_amount_kr 동일 규칙)"""
    s = str(text or "").strip().replace(",", "").replace(" ", "")
    for suffix in ("아데나", "원", "골드", "g", "G"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return int(float(s))
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)만)?(?:(\d+(?:\.\d+)?)천)?(\d+(?:\.\d+)?)?", s)
    if not m or not any(m.groups()):
        return None
    eok, man, cheon, rest = m.groups()
    total = 0.0
    if eok:
        total += float(eok) * 100_000_000
    if man:
        total += float(man) * 10_000
    if cheon:
        total += float(cheon) * 1_000
    if rest:
        total += float(rest)
    return int(total)


def next_bid_price(rec):
    """다음 입찰가: 첫 입찰=시작가, 이후 현재가+호가."""
    if not rec.get("bidder_id"):
        return int(rec["start_price"])
    return int(rec["current_price"]) + int(rec["increment"])


def apply_bid(rec, uid, name, now=None):
    """rec에 입찰 반영. 반환: (ok, 안내문). 이미 최고 입찰자면 거부."""
    if rec.get("status") != "active":
        return False, "진행 중인 경매가 아니에요."
    if str(rec.get("bidder_id") or "") == str(uid):
        return False, "이미 최고 입찰자예요."
    price = next_bid_price(rec)
    rec["current_price"] = price
    rec["bidder_id"] = str(uid)
    rec["bidder_name"] = name
    rec["bid_count"] = int(rec.get("bid_count", 0)) + 1
    # 처리 순서가 클릭 순서와 달라도 마감 기준 시각은 뒤로만 간다
    rec["last_bid_at"] = max(float(now if now is not None else time.time()), float(rec.get("last_bid_at") or 0))
    rec.setdefault("bid_history", []).append({"uid": str(uid), "name": name, "price": price,
                                              "ts": float(now if now is not None else time.time()),
                                              "at": datetime.now().isoformat(timespec="seconds")})
    return True, f"🙋 {price:,} 입찰했어요."


BID_HISTORY_SHOW = 5
BID_SETTLE_SEC = 2   # 입찰 후 이만큼 기다렸다가 마감 카운트 시작 (연타 몰림 정리 시간)


def fmt_click_time(entry):
    """입찰 버튼을 누른 순간 (Discord 기준, 밀리초까지) — 'HH:MM:SS.mmm'."""
    ts = (entry or {}).get("ts")
    if ts:
        dt = datetime.fromtimestamp(float(ts))
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return str((entry or {}).get("at") or "")[11:19]


def last_bid_entry(rec):
    hist = (rec or {}).get("bid_history") or []
    return hist[-1] if hist else None


def bid_history_text(rec, n=BID_HISTORY_SHOW):
    """최근 입찰 n건 (최신이 위) — 최고 입찰자가 바뀌는 흐름을 보여 준다."""
    hist = (rec or {}).get("bid_history") or []
    if not hist:
        return ""
    lines = []
    for i, h in enumerate(reversed(hist[-n:])):
        mark = "🥇" if i == 0 else "▫️"
        at = fmt_click_time(h)
        lines.append(f"{mark} {h.get('name', '?')}" + (f" ({at})" if at else "") + f" — {int(h.get('price') or 0):,}")
    return "\n".join(lines)


def close_deadline(rec):
    """마감 예정 unix 시각. 입찰 없으면 시작 + 카운트, 입찰 후엔 마지막 입찰 + 2초 대기 + 카운트. 진행 전이면 None."""
    if rec.get("status") != "active":
        return None
    if rec.get("last_bid_at"):
        return float(rec["last_bid_at"]) + BID_SETTLE_SEC + int(rec["countdown_sec"])
    if not rec.get("started_ts"):
        return None
    return float(rec["started_ts"]) + int(rec["countdown_sec"])


def bid_lock_until(rec):
    """입찰 직후 잠금 해제 시각 (= 마감 카운트 시작). 이 사이 클릭은 받지 않아 동시 연타로 순서가 꼬이지 않는다."""
    if (rec or {}).get("status") != "active" or not rec.get("last_bid_at"):
        return None
    return float(rec["last_bid_at"]) + BID_SETTLE_SEC


def live_block(rec, unit="", now=None):
    """진행 중 경매의 큰 글씨 부분: 최고 입찰자(누른 시각) · 현재가 · 마감 카운트."""
    now = time.time() if now is None else now
    started = float(rec.get("started_ts") or 0)
    if started > now:
        return f"## ⏳ 시작 <t:{int(started) + 1}:R>"
    lines = []
    if rec.get("bidder_id"):
        at = fmt_click_time(last_bid_entry(rec))
        lines.append(f"## 🥇 {rec.get('bidder_name')}" + (f" ({at})" if at else ""))
        lines.append(f"## 💰 {int(rec.get('current_price') or 0):,}{unit}")
    else:
        lines.append("### 💰 아직 입찰 없음")
    dl = close_deadline(rec)
    if dl:
        lines.append(f"## ⏱️ 마감 <t:{int(dl) + 1}:R>")
        lines.append(f"-# 입찰이 들어오면 {BID_SETTLE_SEC}초 동안 입찰 버튼이 잠기고, 그 뒤 {int(rec['countdown_sec'])}초 카운트 · 새 입찰이 오면 다시 시작")
    return "\n".join(lines)


class AuctionStore:
    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger
        self._data = None

    def load(self):
        if self._data is None:
            data = {}
            if os.path.exists(self.path):
                for attempt in range(4):
                    try:
                        with open(self.path, encoding="utf-8") as f:
                            data = json.load(f)
                        break
                    except Exception as e:
                        if attempt < 3:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        # 손상 — 빈 상태로 덮어쓰기 전에 원본을 따로 보관 (진행 중 경매 기록 보존)
                        data = {}
                        try:
                            import shutil
                            shutil.copy2(self.path, f"{self.path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}")
                        except Exception:
                            pass
                        if self.log:
                            self.log.info(f"[경매 상태 파일 손상 — 원본 보관 후 새로 시작] {type(e).__name__}: {e}")
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def get(self, mid):
        return self.load().get(str(mid))

    def save(self, mid, rec):
        data = self.load()
        data[str(mid)] = rec
        write_json_atomic(self.path, data, self.log)


def build_embed(rec, currency=""):
    unit = f" {currency}" if currency else ""
    item = rec.get("item", "?")
    status = rec.get("status")
    bidder = rec.get("bidder_name") or ""
    price = int(rec.get("current_price") or 0)
    if status == "closed":
        embed = discord.Embed(title=f"🏁 경매마감 — {item}", color=0x1d9e75,
                              description="**🔒 경매가 마감되었습니다.**")
        if bidder:
            embed.add_field(name="🏆 최종 낙찰자", value=f"**{bidder}**", inline=True)
            embed.add_field(name="💰 낙찰 금액", value=f"**{price:,}{unit}**", inline=True)
        else:
            embed.add_field(name="결과", value="유찰 (입찰 없음)", inline=False)
    else:
        if status == "ready":
            embed = discord.Embed(title=f"⏸️ 경매 대기 — {item}",
                                  description="▶️ 경매시작을 누르면 입찰이 열립니다.", color=0x378add)
        else:
            embed = discord.Embed(title=f"🔨 경매 진행중 — {item}", description=live_block(rec, unit),
                                  color=0xf0b232)
        embed.add_field(name="시작가", value=f"{int(rec.get('start_price', 0)):,}{unit}", inline=True)
        embed.add_field(name="호가", value=f"{int(rec.get('increment', 0)):,}{unit}", inline=True)
        embed.add_field(name="카운트", value=f"{rec.get('countdown_sec')}초", inline=True)
        hist = bid_history_text(rec)
        if hist and status == "active":
            embed.add_field(name=f"📜 최근 입찰 (총 {rec.get('bid_count', 0)}회)", value=hist, inline=False)
    if rec.get("source_message_id") and status != "closed":
        embed.add_field(name="🔐 입찰 자격", value="해당 보스탐 참석자만 입찰할 수 있어요.", inline=False)
    embed.set_footer(text=f"입찰 {rec.get('bid_count', 0)}회 · 등록자 {rec.get('creator_name', '-')}")
    return embed


class BossAuction:
    """보스봇 경매 런타임 (View/Modal 콜백에서 사용)."""

    def __init__(self, bot, *, channel_id, data_dir, is_admin, logger, currency="", get_attendees=None):
        self.bot = bot
        self.get_attendees = get_attendees   # (보스 알림 메시지ID) → 참석자 UID 목록
        self.channel_id = int(channel_id or 0)
        self.store = AuctionStore(os.path.join(data_dir, "boss_auction_state.json"), logger)
        self.is_admin = is_admin
        self.log = logger
        self.currency = currency
        self.lock = asyncio.Lock()
        self._close_tasks = {}
        self._refresh_tasks = {}   # 메시지ID → 연타 후 마지막 상태로 다시 그리는 예약
        self.clock_offset = 0.0   # PC 시계 − Discord 시계 (초)
        # 경매 진행판 전체 종료 콜백 async (board) -> None (보스 알림에 결과 반영)
        self.on_board_finished = None
        # 진행판 생성·아이템 마감 등 경매 대기 목록이 바뀔 때 호출 (동기 콜백, 예약만 해야 함)
        self.on_change = None
        self.board_defaults = dict(DEFAULT_BOARD_DEFAULTS)   # 재경매 입력에서 숫자 생략 시

    def click_ts(self, interaction):
        """버튼을 누른 순간의 Discord 시각(unix). PC 시계 차이도 함께 갱신."""
        ts = interaction.created_at.timestamp()
        # 처리 지연은 항상 양수 → 관측값 중 최소가 실제 시계 차이에 가장 가깝다 (최근 값으로 서서히 갱신)
        observed = time.time() - ts
        self.clock_offset = observed if abs(observed - self.clock_offset) > 2 else min(observed, self.clock_offset + 0.05)
        return ts

    def discord_now(self):
        return time.time() - self.clock_offset

    def safe_save(self, mid, rec):
        """저장 실패해도 입찰은 메모리에 남아 다음 저장 때 함께 기록된다 — 클릭 처리를 막지 않는다."""
        try:
            self.store.save(mid, rec)
        except Exception as e:
            self.log.info(f"[경매 저장 실패 — 다음 저장 때 재기록] {mid}: {type(e).__name__}: {e}")

    async def respond_ephemeral(self, interaction, text):
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            self.log.info(f"[경매 응답 실패] {type(e).__name__}: {e}")

    async def respond_edit(self, interaction, **kwargs):
        """버튼 응답으로 메시지 갱신. 응답 시한을 넘겼으면 메시지를 직접 수정 (입찰은 이미 반영됨)."""
        try:
            await interaction.response.edit_message(**kwargs)
            return
        except (discord.NotFound, discord.HTTPException) as e:
            self.log.info(f"[경매 응답 지연 → 메시지 직접 수정] {type(e).__name__}: {e}")
        try:
            await interaction.message.edit(**kwargs)
        except Exception as e:
            self.log.info(f"[경매 메시지 수정 실패] {type(e).__name__}: {e}")

    def schedule_refresh(self, message, delay=None):
        """저장된 최신 상태로 메시지를 한 번 더 그린다 — 입찰 잠금이 풀리는 순간(카운트 시작)에 맞춰 버튼을 다시 연다.
        클릭마다 보내는 수정 요청은 Discord에 도착하는 순서가 뒤바뀔 수 있어, 앞선 클릭의 화면이
        마지막에 적용되면 최종 입찰자가 이전 사람으로 보인다 — 이 갱신이 최종 상태로 덮어쓴다."""
        key = str(message.id)
        old = self._refresh_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        if delay is None:
            until = self.lock_until(self.store.get(message.id))
            delay = max(REFRESH_DELAY_SEC, until - self.discord_now() + 0.15) if until else REFRESH_DELAY_SEC

        async def _run():
            try:
                await asyncio.sleep(max(0.0, delay))
                for attempt in range(3):
                    latest = self.store.get(message.id)
                    if not latest:
                        return
                    if latest.get("type") == "board":
                        embed = build_board_embed(latest, self.currency, self.discord_now())
                    else:
                        embed = build_embed(latest, self.currency)
                    try:
                        await message.edit(embed=embed, view=self.view_for(latest))
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # 실패하면 버튼이 잠긴 채 남으니 다시 시도
                        self.log.info(f"[경매 최종 갱신 실패 {attempt + 1}/3] {key}: {type(e).__name__}: {e}")
                        await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                pass
            finally:
                if self._refresh_tasks.get(key) is task:
                    self._refresh_tasks.pop(key, None)

        task = asyncio.ensure_future(_run())
        self._refresh_tasks[key] = task

    def lock_until(self, rec):
        target = board_current(rec) if (rec or {}).get("type") == "board" else rec
        return bid_lock_until(target) if target else None

    def view_for(self, rec):
        until = self.lock_until(rec)
        locked = until is not None and self.discord_now() < until
        if (rec or {}).get("type") == "board":
            return BoardView(self, state=board_state(rec), relist=board_can_relist(rec), locked=locked)
        return AuctionView(self, state=(rec or {}).get("status", "closed"), locked=locked)

    async def load_allowed(self, source_message_id):
        """원본 보스 알림의 참석자 UID 스냅샷 (Excel 조회는 스레드에서 — 입찰 반응 지연 방지)."""
        if not source_message_id or self.get_attendees is None:
            return None
        try:
            uids = await asyncio.to_thread(self.get_attendees, source_message_id)
        except Exception as e:
            self.log.info(f"[경매 참석자 조회 실패] 알림 {source_message_id}: {type(e).__name__}: {e}")
            return None
        return [str(u) for u in (uids or [])]

    @staticmethod
    def can_bid(rec, uid):
        """입찰 자격: 원본 보스 알림이 연결된 경매는 그 알림의 참석자(시작 시점 스냅샷)만."""
        allowed = rec.get("allowed_uids")
        if not rec.get("source_message_id") or allowed is None:
            return True
        return str(uid) in allowed

    async def create(self, item, countdown_sec, start_price, increment, creator, source_message_id=None):
        if not self.channel_id:
            return False, "❌ 경매 채널이 설정되지 않았어요. (boss_config: auction_channel_id)"
        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                return False, "❌ 경매 채널을 찾을 수 없어요. 봇의 채널 보기 권한을 확인해 주세요."
        rec = {
            "item": item, "countdown_sec": int(countdown_sec),
            "start_price": int(start_price), "increment": int(increment),
            "current_price": 0, "bidder_id": None, "bidder_name": "", "bid_count": 0,
            "last_bid_at": None, "status": "ready", "channel_id": self.channel_id,
            "creator_id": str(creator.id), "creator_name": creator.display_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_message_id": str(source_message_id) if source_message_id else None,
            "allowed_uids": await self.load_allowed(source_message_id),
        }
        try:
            msg = await ch.send(embed=build_embed(rec, self.currency), view=self.view_for(rec))
        except discord.Forbidden:
            return False, (f"❌ 경매 채널 <#{self.channel_id}> 에 메시지를 보낼 권한이 없어요. "
                           f"봇에게 채널 보기·메시지 보내기·링크 첨부 권한을 주세요.")
        self.store.save(msg.id, rec)
        self.log.info(f"[경매 등록] {item} | 시작가 {start_price:,} 호가 {increment:,} 카운트 {countdown_sec}초 "
                      f"| 등록자 {creator.display_name} | msg {msg.id}")
        return True, f"✅ 경매 세팅을 등록했어요 → {msg.jump_url}\n경매 메시지에서 ▶️ 경매시작을 누르면 시작돼요."

    def _schedule_close(self, mid, rec):
        dl = close_deadline(rec)
        if dl is None:
            return
        old = self._close_tasks.pop(str(mid), None)
        if old and not old.done():
            old.cancel()

        async def _run():
            try:
                await asyncio.sleep(max(0.0, dl + CLOSE_GRACE_SEC - self.discord_now()))
                await self.close(mid, expected_bids=rec.get("bid_count"))
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.log.info(f"[경매 자동 마감 실패] {mid}: {type(e).__name__}: {e}")

        self._close_tasks[str(mid)] = asyncio.ensure_future(_run())

    async def close(self, mid, *, expected_bids=None, by=None):
        """낙찰/유찰 처리. expected_bids가 주어지면 그 사이 새 입찰이 있었을 때 무시."""
        async with self.lock:
            rec = self.store.get(mid)
            if not rec or rec.get("status") == "closed":
                return None
            if expected_bids is not None and rec.get("bid_count") != expected_bids:
                return None
            rec["status"] = "closed"
            rec["closed_at"] = datetime.now().isoformat(timespec="seconds")
            if by:
                rec["closed_by"] = by
            self.store.save(mid, rec)
        self._close_tasks.pop(str(mid), None)
        try:
            ch = self.bot.get_channel(int(rec["channel_id"])) or await self.bot.fetch_channel(int(rec["channel_id"]))
            msg = await ch.fetch_message(int(mid))
            await msg.edit(embed=build_embed(rec, self.currency), view=self.view_for(rec))
            if rec.get("bidder_id"):
                await ch.send(f"🏁 **{rec['item']}** 낙찰 — <@{rec['bidder_id']}> **{int(rec['current_price']):,}**"
                              f"{' ' + self.currency if self.currency else ''}",
                              reference=msg, mention_author=False)
        except Exception as e:
            self.log.info(f"[경매 마감 메시지 갱신 실패] {mid}: {type(e).__name__}: {e}")
        self.log.info(f"[경매 마감] {rec.get('item')} | 낙찰 {rec.get('bidder_name') or '유찰'} "
                      f"{int(rec.get('current_price') or 0):,} | 입찰 {rec.get('bid_count', 0)}회 | msg {mid}")
        return rec

    async def watch_overdue(self, interval=3.0, slack=3.0):
        """마감 시각이 지났는데 마감 처리가 안 된 경매를 찾아 마감 (타이머가 취소·유실돼도 카운트가 멈춘 채 남지 않게)."""
        while True:
            await asyncio.sleep(interval)
            try:
                now = self.discord_now()
                for mid, rec in list(self.store.load().items()):
                    board = rec.get("type") == "board"
                    target = board_current(rec) if board else rec
                    if not target or (board and rec.get("status") != "active"):
                        continue
                    dl = close_deadline(target)
                    if dl is None or now < dl + CLOSE_GRACE_SEC + slack:
                        continue
                    task = self._close_tasks.get(str(mid))
                    if task and not task.done():
                        continue
                    self.log.info(f"[경매 마감 점검] 마감 지난 경매 처리 | msg {mid}")
                    if board:
                        await self.advance_board(mid, expected=(rec.get("current"), target.get("bid_count")))
                    else:
                        await self.close(mid, expected_bids=rec.get("bid_count"))
            except Exception as e:
                self.log.info(f"[경매 마감 점검 실패] {type(e).__name__}: {e}")

    async def restore(self):
        """재시작 후 진행 중 경매의 마감 타이머 재등록 (마감 지난 건 즉시 낙찰)."""
        for mid, rec in list(self.store.load().items()):
            if rec.get("type") == "board":
                if board_state(rec) == "active":
                    self._schedule_board(mid, rec)
                    self.log.info(f"[진행판 복구] {rec.get('boss_name')} {rec.get('boss_time')} | msg {mid}")
                elif board_can_relist(rec) and not rec.get("relist_button"):
                    # 이전 버전에서 끝난 진행판에 🔁 유찰 재경매 버튼을 한 번 붙여 준다
                    try:
                        ch = self.bot.get_channel(int(rec["channel_id"])) or await self.bot.fetch_channel(int(rec["channel_id"]))
                        msg = await ch.fetch_message(int(mid))
                        await msg.edit(view=self.view_for(rec))
                    except Exception as e:
                        self.log.info(f"[진행판 재경매 버튼 추가 실패] {mid}: {type(e).__name__}: {e}")
                    rec["relist_button"] = True
                    self.store.save(mid, rec)
            elif rec.get("status") == "active":
                self._schedule_close(mid, rec)
                self.log.info(f"[경매 복구] {rec.get('item')} | msg {mid}")

    # ── 경매 진행판 ──────────────────────────────────────────────────────
    async def _auction_channel(self):
        if not self.channel_id:
            return None, "❌ 경매 채널이 설정되지 않았어요. (boss_config: auction_channel_id)"
        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                return None, "❌ 경매 채널을 찾을 수 없어요. 봇의 채널 보기 권한을 확인해 주세요."
        return ch, ""

    def find_open_board(self, source_message_id):
        """같은 보스 알림에서 아직 진행 중인 진행판 메시지ID (없으면 None)."""
        for mid, rec in self.store.load().items():
            if (rec.get("type") == "board" and rec.get("status") == "active"
                    and str(rec.get("source_message_id")) == str(source_message_id)):
                return mid
        return None

    def notify_change(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                self.log.info(f"[경매 변경 알림 실패] {type(e).__name__}: {e}")

    def boards_for_source(self, source_message_id):
        """같은 보스 알림에서 만든 진행판 전부 (등록 순). 재경매 결과 합치기용."""
        boards = [rec for rec in self.store.load().values()
                  if rec.get("type") == "board" and str(rec.get("source_message_id")) == str(source_message_id)]
        return sorted(boards, key=lambda b: b.get("created_at") or "")

    async def create_board(self, lots, *, boss_name, boss_time, source_message_id, creator):
        """진행판을 대기 상태로 게시 (관리자가 ▶️ 경매시작을 눌러야 아이템별로 시작). 반환: (ok, 안내문, 메시지)."""
        ch, err = await self._auction_channel()
        if ch is None:
            return False, err, None
        board = {
            "type": "board", "status": "active", "current": None, "lots": lots,
            "boss_name": boss_name, "boss_time": boss_time, "channel_id": self.channel_id,
            "source_message_id": str(source_message_id) if source_message_id else None,
            "allowed_uids": await self.load_allowed(source_message_id),
            "creator_id": str(creator.id), "creator_name": creator.display_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            msg = await ch.send(embed=build_board_embed(board, self.currency, self.discord_now()),
                                view=self.view_for(board))
        except discord.Forbidden:
            return False, (f"❌ 경매 채널 <#{self.channel_id}> 에 메시지를 보낼 권한이 없어요. "
                           f"봇에게 채널 보기·메시지 보내기·링크 첨부 권한을 주세요."), None
        self.store.save(msg.id, board)
        self.notify_change()
        self.log.info(f"[진행판 등록] {boss_name} {boss_time} | {len(lots)}건 | 등록자 {creator.display_name} | msg {msg.id}")
        summary = "\n".join(f"· {lot_label(x)} — 시작 {fmt_amount_kr(x['start_price'])} / 호가 "
                            f"{fmt_amount_kr(x['increment'])} / {int(x['countdown_sec'])}초" for x in lots[:10])
        if len(lots) > 10:
            summary += f"\n· … 외 {len(lots) - 10}건"
        return True, (f"🔨 경매 {len(lots)}건 진행판 → {msg.jump_url}\n{summary}\n"
                      f"관리자가 ▶️ 경매시작을 누르면 아이템별로 시작돼요."), msg

    def _schedule_board(self, mid, board):
        lot = board_current(board)
        dl = close_deadline(lot) if lot else None
        old = self._close_tasks.pop(str(mid), None)
        if old and not old.done():
            old.cancel()
        if dl is None:
            return
        expected = (board.get("current"), lot.get("bid_count"))

        async def _run():
            try:
                await asyncio.sleep(max(0.0, dl + CLOSE_GRACE_SEC - self.discord_now()))
                await self.advance_board(mid, expected=expected)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.log.info(f"[진행판 자동 진행 실패] {mid}: {type(e).__name__}: {e}")

        self._close_tasks[str(mid)] = asyncio.ensure_future(_run())

    async def start_next_lot(self, mid, start_ts, allowed=None):
        """관리자 ▶️ — 다음 대기 아이템 시작. 반환: (lot | None, 안내문)."""
        async with self.lock:
            board = self.store.get(mid)
            state = board_state(board) if board else "closed"
            if state == "closed":
                return None, "경매가 끝났어요."
            if state == "active":
                return None, "지금 경매 중인 아이템이 있어요. 마감된 뒤 눌러 주세요."
            if allowed is not None:
                board["allowed_uids"] = allowed
            lot = board_start_next(board, start_ts)
            self.store.save(mid, board)
        if lot is not None:
            self._schedule_board(mid, board)
        return lot, ""

    async def advance_board(self, mid, *, expected=None, by=None, stop=False):
        """현재 아이템 마감(낙찰/유찰) 후 다음 ▶️ 대기. stop=True 면 남은 아이템 취소 후 종료.
        expected=(현재 순번, 입찰 수)가 주어지면 그 사이 입찰·진행이 있었을 때 무시. 반환: 마감된 lot."""
        async with self.lock:
            board = self.store.get(mid)
            if not board or board.get("status") == "closed":
                return None
            lot = board_current(board)
            if expected is not None and (board.get("current"), (lot or {}).get("bid_count")) != expected:
                return None
            closed_lot = board_close_current(board, by=by)
            if stop:
                for x in board.get("lots") or []:
                    if x.get("status") == "pending":
                        x["status"] = "cancelled"
            if closed_lot is None and not stop:
                return None
            finished = board_after_close(board)
            self.store.save(mid, board)
        old = self._close_tasks.pop(str(mid), None)
        if old and not old.done() and stop:
            old.cancel()
        try:
            ch = self.bot.get_channel(int(board["channel_id"])) or await self.bot.fetch_channel(int(board["channel_id"]))
            msg = await ch.fetch_message(int(mid))
            await msg.edit(embed=build_board_embed(board, self.currency, self.discord_now()), view=self.view_for(board))
            if closed_lot and closed_lot.get("bidder_id"):
                await ch.send(f"🏁 **{lot_label(closed_lot)}** 낙찰 — <@{closed_lot['bidder_id']}> "
                              f"**{int(closed_lot['current_price']):,}**{' ' + self.currency if self.currency else ''}",
                              reference=msg, mention_author=False)
        except Exception as e:
            self.log.info(f"[진행판 메시지 갱신 실패] {mid}: {type(e).__name__}: {e}")
        if closed_lot or stop:
            self.notify_change()
        if closed_lot:
            self.log.info(f"[진행판 마감] {lot_label(closed_lot)} | 낙찰 {closed_lot.get('bidder_name') or '유찰'} "
                          f"{int(closed_lot.get('current_price') or 0):,} | 입찰 {closed_lot.get('bid_count', 0)}회 | msg {mid}")
        if finished and self.on_board_finished:
            try:
                await self.on_board_finished(board)
            except Exception as e:
                self.log.info(f"[진행판 결과 반영 실패] {mid}: {type(e).__name__}: {e}")
        return closed_lot


class AuctionSetupModal(ui.Modal, title="경매 세팅"):
    def __init__(self, auction: BossAuction, item_default="", source_message_id=None):
        super().__init__()
        self.auction = auction
        self.source_message_id = source_message_id
        self.item = ui.TextInput(label="물품", default=item_default[:100], max_length=100)
        self.countdown = ui.TextInput(label="카운트 (초) — 이 시간 동안 입찰 없으면 마감", default="5", max_length=4)
        self.start_price = ui.TextInput(label="시작가", placeholder="예: 80만, 1.5억, 3,750,000", max_length=30)
        self.increment = ui.TextInput(label="호가 (입찰 시 오르는 금액)", placeholder="예: 10만", max_length=30)
        for x in (self.item, self.countdown, self.start_price, self.increment):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        item = self.item.value.strip()
        try:
            countdown = int(self.countdown.value.strip())
        except ValueError:
            countdown = 0
        start_price = parse_amount_kr(self.start_price.value)
        increment = parse_amount_kr(self.increment.value)
        err = None
        if not item:
            err = "❌ 물품을 입력해 주세요."
        elif not 1 <= countdown <= 3600:
            err = "❌ 카운트는 1~3600초 사이 숫자로 입력해 주세요."
        elif not start_price or not increment or start_price <= 0 or increment <= 0:
            err = f"❌ 시작가·호가를 확인해 주세요. {AMOUNT_HINT}"
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _, text = await self.auction.create(item, countdown, start_price, increment, interaction.user,
                                            source_message_id=self.source_message_id)
        await interaction.followup.send(text, ephemeral=True)


class AuctionView(ui.View):
    """경매 메시지 버튼 (persistent). 상태: ready / active / closed."""

    def __init__(self, auction: BossAuction, state="ready", locked=False):
        super().__init__(timeout=None)
        self.auction = auction
        for child in self.children:
            cid = getattr(child, "custom_id", "")
            if cid == "boss_auc_start":
                child.disabled = state != "ready"
            elif cid == "boss_auc_bid":
                child.disabled = state != "active" or locked
                if locked and state == "active":
                    child.label, child.emoji = "잠시 대기", "⏳"
            elif cid == "boss_auc_stop":
                child.disabled = state == "closed"

    @ui.button(label="경매시작", emoji="▶️", style=discord.ButtonStyle.primary, custom_id="boss_auc_start")
    async def start_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        if not a.is_admin(interaction.user):
            await interaction.response.send_message("⛔ 경매 시작 권한이 없어요.", ephemeral=True)
            return
        mid = interaction.message.id
        pre = a.store.get(mid) or {}
        # 시작 시점 참석자로 입찰 자격 갱신 (등록 후 인원체크로 추가된 참석자 반영)
        allowed = await a.load_allowed(pre.get("source_message_id"))
        async with a.lock:
            rec = a.store.get(mid)
            if allowed is not None and rec:
                rec["allowed_uids"] = allowed
            if not rec or rec.get("status") != "ready":
                await interaction.response.send_message("이미 시작했거나 종료된 경매예요.", ephemeral=True)
                return
            rec["status"] = "active"
            rec["started_at"] = datetime.now().isoformat(timespec="seconds")
            rec["started_ts"] = a.click_ts(interaction)
            a.store.save(mid, rec)
            await interaction.response.edit_message(embed=build_embed(rec, a.currency), view=a.view_for(rec))
            a._schedule_close(mid, rec)
        a.log.info(f"[경매 시작] {rec.get('item')} | by {interaction.user.display_name} | msg {mid}")

    @ui.button(label="입찰", emoji="🙋", style=discord.ButtonStyle.success, custom_id="boss_auc_bid")
    async def bid_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        mid = interaction.message.id
        clicked = a.click_ts(interaction)   # 판정 기준 = 누른 순간 (처리 지연과 무관)
        # 잠금 안에서는 판정·저장만 하고 Discord 응답은 밖에서 — 연타가 몰려도 뒤 클릭이 3초 응답 시한을 넘기지 않도록
        reject, rec = None, None
        async with a.lock:
            rec = a.store.get(mid)
            if not rec:
                reject = "경매 정보를 찾을 수 없어요."
            else:
                dl = close_deadline(rec)
                if dl is not None and clicked >= dl:
                    reject = "⏰ 카운트가 끝난 뒤 눌렀어요. 마감 처리됩니다."
                elif (until := bid_lock_until(rec)) and clicked < until and str(rec.get("bidder_id")) != str(interaction.user.id):
                    reject = (f"⏳ 방금 **{rec.get('bidder_name')}**님 입찰이 먼저 들어왔어요. "
                              f"{BID_SETTLE_SEC}초 뒤 카운트가 시작되면 다시 눌러 주세요.")
                elif rec.get("status") == "active" and not a.can_bid(rec, interaction.user.id):
                    reject = "🔐 이 경매는 해당 보스탐 참석자만 입찰할 수 있어요."
                else:
                    ok, text = apply_bid(rec, interaction.user.id, interaction.user.display_name, now=clicked)
                    if not ok:
                        reject = text
                    else:
                        a.safe_save(mid, rec)
                        a._schedule_close(mid, rec)
                        price, count = int(rec["current_price"]), rec["bid_count"]
        if reject:
            await a.respond_ephemeral(interaction, reject)
            return
        a.log.info(f"[경매 입찰] {rec.get('item')} | {interaction.user.display_name}({interaction.user.id}) "
                   f"{price:,} | {count}회 | msg {mid}")
        latest = a.store.get(mid) or rec   # 응답 순서가 뒤바뀌어도 항상 최신 상태로 그린다
        await a.respond_edit(interaction, embed=build_embed(latest, a.currency), view=a.view_for(latest))
        a.schedule_refresh(interaction.message)   # 연타 뒤 최종 입찰자로 확정 표시

    @ui.button(label="경매종료", emoji="⏹️", style=discord.ButtonStyle.secondary, custom_id="boss_auc_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        if not a.is_admin(interaction.user):
            await interaction.response.send_message("⛔ 경매 종료 권한이 없어요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        rec = await a.close(interaction.message.id, by=interaction.user.display_name)
        text = "이미 종료된 경매예요." if rec is None else (
            f"⏹️ 경매를 종료했어요 — 낙찰 {rec['bidder_name']} {int(rec['current_price']):,}"
            if rec.get("bidder_id") else "⏹️ 경매를 종료했어요 (유찰).")
        await interaction.followup.send(text, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════
#  경매 진행판 — 한 보스탐에서 나온 여러 아이템을 메시지 하나에서 순서대로 경매
# ══════════════════════════════════════════════════════════════════════════
#  보스 알림의 💎 득템·경매 입력 한 번으로 '대기' 상태로 생성된다. 자동 진행은 없고,
#  관리자가 ▶️ 경매시작을 누를 때마다 다음 아이템 하나가 시작된다. 아이템(lot)마다
#  시작가·호가·카운트가 다를 수 있고, 카운트 동안 입찰이 없으면 그 아이템만 마감된다.
#  lot 은 단일 경매 rec 과 같은 키를 써서 next_bid_price/apply_bid/close_deadline 을 재사용한다.
#  저장: boss_auction_state.json 에 {진행판 메시지ID: board} (board["type"] == "board").

MAX_BOARD_LOTS = 25
MAX_LOT_QTY = 20
DEFAULT_BOARD_DEFAULTS = {"start_price": 100_000, "increment": 10_000, "countdown_sec": 5}


def fmt_amount_kr(value):
    """1300000 → '130만', 150000000 → '1억5,000만', 단위로 안 떨어지면 콤마 표기."""
    v = int(value or 0)
    if v >= 100_000_000 and v % 10_000 == 0:
        eok, man = divmod(v // 10_000, 10_000)
        return f"{eok:,}억" + (f"{man:,}만" if man else "")
    if v >= 10_000 and v % 10_000 == 0:
        return f"{v // 10_000:,}만"
    return f"{v:,}"


def _amount_token(token):
    """줄 끝 금액 토큰 판정 — 단위(억/만/천)가 있거나 1,000 이상인 숫자만 금액으로 본다
    (아이템 이름 끝의 작은 숫자, 예: '룬 3' 을 금액으로 오인하지 않도록)."""
    amt = parse_amount_kr(token)
    if amt is None or amt <= 0:
        return None
    if re.search(r"[억만천]", token) or amt >= 1000:
        return amt
    return None


def parse_board_defaults(text, fallback=None):
    """'10만 1만 5초' → {start_price, increment, countdown_sec}. 빠진 값은 fallback."""
    out = dict(fallback or DEFAULT_BOARD_DEFAULTS)
    amounts = []
    for tok in str(text or "").replace("/", " ").replace(",", " ").split():
        m = re.fullmatch(r"(\d+)초", tok)
        if m:
            out["countdown_sec"] = int(m.group(1))
            continue
        if len(amounts) >= 2 and _plain_int(tok) is not None and _plain_int(tok) <= 3600:
            out["countdown_sec"] = _plain_int(tok)   # '10만 1만 5' — 초 빠진 카운트
            continue
        amt = parse_amount_kr(tok)
        if amt:
            amounts.append(amt)
    if amounts:
        out["start_price"] = amounts[0]
    if len(amounts) > 1:
        out["increment"] = amounts[1]
    return out


def format_board_defaults(d):
    return f"{fmt_amount_kr(d['start_price'])} {fmt_amount_kr(d['increment'])} {int(d['countdown_sec'])}초"


def _plain_int(token):
    """단위 없는 정수 토큰이면 값, 아니면 None ('5', '1,000' → 정수 / '5만' → None)."""
    t = token.replace(",", "")
    return int(t) if t.isdigit() else None


def parse_lot_line(line, defaults):
    """'반지 x2 30만 5만 10초' → (이름, 수량, 시작가, 호가, 카운트). 해석 불가 시 ValueError.

    줄 끝의 숫자들을 순서대로 시작가 → 호가 → 카운트로 본다. '초'가 빠진 카운트(`100만 10만 5`),
    쉼표·슬래시 구분(`100만,10만,5초`), '시작가/호가/카운트' 같은 말머리도 받아들인다.
    이름 끝의 작은 숫자 하나(`룬 3`)는 금액이 아니라 이름으로 남긴다."""
    text = str(line or "")
    text = re.sub(r"(시작가|시작|호가|카운트)\s*[:=]?", " ", text)
    text = text.replace("/", " ")
    text = re.sub(r",(?!\d{3}(?!\d))", " ", text)   # 1,000,000 의 천 단위 쉼표는 남김
    tokens = text.split()
    run = []   # 줄 끝 숫자 토큰 (뒤에서부터)
    while len(tokens) > 1:
        t = tokens[-1]
        if re.fullmatch(r"\d+초", t) or re.fullmatch(r"(?:[xX×*]\d+|\d+개)", t) or (parse_amount_kr(t) or 0) > 0:
            run.insert(0, tokens.pop())
        else:
            break
    qty, countdown, amts = 1, None, []
    for t in run:
        m_sec = re.fullmatch(r"(\d+)초", t)
        m_qty = re.fullmatch(r"(?:[xX×*](\d+)|(\d+)개)", t)
        if m_sec and countdown is None:
            countdown = int(m_sec.group(1))
        elif m_qty and qty == 1:
            qty = int(m_qty.group(1) or m_qty.group(2))
        else:
            amts.append(t)
    # '초' 없는 카운트: 숫자가 셋이거나, 금액 뒤에 붙은 작은 정수면 마지막을 카운트로
    if countdown is None and len(amts) >= 2:
        last = _plain_int(amts[-1])
        prev = parse_amount_kr(amts[-2]) or 0
        if last is not None and last <= 3600 and (len(amts) >= 3 or (last < 1000 and prev >= 1000)):
            countdown = last
            amts.pop()
    back = []
    while len(amts) > 2:
        back.append(amts.pop(0))
    # 이름 끝 작은 숫자 ('룬 3', '룬 3 10만') — 금액 하나뿐이거나 뒤에 더 큰 금액이 있으면 이름으로
    if amts and _plain_int(amts[0]) is not None and _plain_int(amts[0]) < 1000:
        if len(amts) == 1 or (parse_amount_kr(amts[1]) or 0) >= 1000 and _plain_int(amts[1]) is None:
            back.append(amts.pop(0))
    name = " ".join(tokens + back).strip()
    m = re.fullmatch(r"(.*?)\s*[xX×*](\d+)", name)
    if m and m.group(1) and qty == 1:
        name, qty = m.group(1).strip(), int(m.group(2))
    if not name:
        raise ValueError(f"아이템 이름이 없어요: `{line}`")
    values = [parse_amount_kr(t) for t in amts]
    start = values[0] if values else int(defaults["start_price"])
    inc = values[1] if len(values) > 1 else int(defaults["increment"])
    cd = countdown if countdown is not None else int(defaults["countdown_sec"])
    if not 1 <= qty <= MAX_LOT_QTY:
        raise ValueError(f"수량은 1~{MAX_LOT_QTY}개로 적어 주세요: `{line}`")
    if not 1 <= cd <= 3600:
        raise ValueError(f"카운트는 1~3600초로 적어 주세요: `{line}`")
    if start <= 0 or inc <= 0:
        raise ValueError(f"시작가·호가를 확인해 주세요: `{line}`")
    return name, qty, start, inc, cd


def parse_board_lots(text, defaults):
    """여러 줄 → lot 목록 (수량만큼 펼침). 반환: (lots, 이름 목록 'xN' 포함)."""
    lots, names = [], []
    for raw in re.split(r"[\n]+", str(text or "")):
        line = raw.strip().strip(",")
        if not line:
            continue
        # 재경매 입력의 '반지 (2/2) …' — 순번을 그대로 유지 (결과 합칠 때 같은 아이템으로 겹쳐 씀)
        fixed_unit = ""
        m = re.search(r"\s*\((\d+/\d+)\)", line)
        if m:
            fixed_unit = m.group(1)
            line = (line[:m.start()] + line[m.end():]).strip()
        name, qty, start, inc, cd = parse_lot_line(line, defaults)
        if fixed_unit:
            qty = 1
        names.append(f"{name} x{qty}" if qty > 1 else name)
        for i in range(qty):
            lots.append({
                "item": name, "unit": fixed_unit or (f"{i + 1}/{qty}" if qty > 1 else ""),
                "start_price": start, "increment": inc, "countdown_sec": cd,
                "current_price": 0, "bidder_id": None, "bidder_name": "", "bid_count": 0,
                "last_bid_at": None, "started_ts": None, "status": "pending",
            })
    if len(lots) > MAX_BOARD_LOTS:
        raise ValueError(f"경매는 한 번에 {MAX_BOARD_LOTS}개까지 올릴 수 있어요 (지금 {len(lots)}개).")
    return lots, names


def make_lot(item, unit, start_price, increment, countdown_sec):
    return {
        "item": item, "unit": unit or "",
        "start_price": int(start_price), "increment": int(increment), "countdown_sec": int(countdown_sec),
        "current_price": 0, "bidder_id": None, "bidder_name": "", "bid_count": 0,
        "last_bid_at": None, "started_ts": None, "status": "pending",
    }


def expand_loot_units(loot_text):
    """득템 기록 '집행검, 반지 x2' → [('집행검',''), ('반지','1/2'), ('반지','2/2')]."""
    units = []
    for raw in re.split(r"[,\n]+", str(loot_text or "")):
        name = raw.strip()
        if not name:
            continue
        qty = 1
        m = re.fullmatch(r"(.*?)\s*(?:[xX×*](\d+)|(\d+)개)", name)
        if m and m.group(1).strip():
            name, qty = m.group(1).strip(), int(m.group(2) or m.group(3))
        qty = max(1, min(qty, MAX_LOT_QTY))
        for i in range(qty):
            units.append((name, f"{i + 1}/{qty}" if qty > 1 else ""))
    return units


def latest_lots(boards):
    """같은 보스탐 진행판들(등록 순)에서 아이템별 가장 최근 lot."""
    out = {}
    for board in boards:
        for lot in board.get("lots") or []:
            out[lot_label(lot)] = lot
    return out


def auction_options(loot_text, boards, *, include_running=False):
    """🔨 경매생성 목록 — 득템 기록 중 아직 경매 안 했거나 유찰된 아이템.
    낙찰된 아이템은 뺀다. 진행판에서 대기·진행 중인 아이템도 빼고, include_running=True면
    note "진행 중"·running=True로 넣는다 (대기 목록 표시용, 고를 수는 없음). 반환: [{item, unit, label, note}]."""
    latest = latest_lots(boards)
    out = []
    for item, unit in expand_loot_units(loot_text):
        label = f"{item} ({unit})" if unit else item
        lot = latest.get(label)
        st = (lot or {}).get("status")
        if st == "closed" and lot.get("bidder_id"):
            continue
        if st in ("pending", "active"):
            if include_running:
                out.append({"item": item, "unit": unit, "label": label, "note": "진행 중", "running": True})
            continue
        note = "유찰 — 다시 경매" if st in ("closed", "cancelled") else "경매 전"
        out.append({"item": item, "unit": unit, "label": label, "note": note})
    return out


DISCORD_EPOCH_MS = 1420070400000


def snowflake_time(message_id):
    """Discord 메시지 ID → 생성 시각 (datetime, 로컬)."""
    return datetime.fromtimestamp(((int(message_id) >> 22) + DISCORD_EPOCH_MS) / 1000)


def pending_entries(loot_by_mid, boards_by_mid, excluded=None, *, since=None):
    """경매 대기 목록 — 보스 알림별로 아직 낙찰되지 않은 아이템 (경매 전·유찰). 경매장에 올라가 있는 건 뺀다.
    기간 제한 없음: 낙찰되거나 관리자가 🙈로 빼기 전까지 남는다.
    loot_by_mid: {mid: {boss_name, boss_time, loot_text}}, boards_by_mid: {mid: [진행판...]},
    excluded: {mid: [label...]} (목록에서 뺀 아이템). since 지정 시 그 이전 알림은 제외. 최신 알림 먼저."""
    excluded = excluded or {}
    out = []
    for mid, info in loot_by_mid.items():
        if not str(mid).isdigit() or not (info.get("loot_text") or "").strip():
            continue
        when = snowflake_time(mid)
        if since is not None and when < since:
            continue
        hide = set(excluded.get(str(mid)) or [])
        opts = [o for o in auction_options(info.get("loot_text", ""), boards_by_mid.get(str(mid), []))
                if o["label"] not in hide]
        if opts:
            out.append({"mid": str(mid), "boss_name": info.get("boss_name", ""),
                        "boss_time": info.get("boss_time", ""), "when": when, "options": opts})
    out.sort(key=lambda e: e["when"], reverse=True)
    return out


_PENDING_RANK = {"유찰": 0, "경매 전": 1, "진행 중": 2}


def pending_status(opt):
    return opt["note"].split(" —")[0]


def pending_items(entries, *, selectable=False):
    """대기 목록을 아이템 단위로 펼쳐 상태순(유찰 → 경매 전 → 진행 중), 같은 상태는 아이템 이름순.
    selectable=True면 진행 중(고를 수 없음)은 뺀다. 반환: [(entry, option)]."""
    pairs = [(e, o) for e in entries for o in e["options"] if not (selectable and o.get("running"))]
    pairs.sort(key=lambda p: (_PENDING_RANK.get(pending_status(p[1]), 9), p[1]["item"], p[1]["label"],
                              -p[0]["when"].timestamp()))
    return pairs


def build_pending_embed(entries, *, days=None, alert_link=None):
    """📌 경매 대기 목록 embed — 아이템별 상태 (이름·상태만, 금액 없음). alert_link: mid → 보스 알림 링크."""
    embed = discord.Embed(title="📌 경매 대기 목록", color=0x5865f2)
    marks = {"유찰": "⚪", "경매 전": "⏳", "진행 중": "🔨"}
    pairs = pending_items(entries)
    lines, shown, last = [], 0, None
    for e, o in pairs:
        status = pending_status(o)
        if status != last:
            lines.append(f"**{marks.get(status, '•')} {status}**")
            last = status
        src = f"{e['boss_name']} {e['boss_time']} · {e['when'].strftime('%m-%d')}"
        if alert_link:
            src = f"[{src}]({alert_link(e['mid'])})"
        line = f"　{o['label']} — {src}"
        if sum(len(x) + 1 for x in lines) + len(line) > 3800:
            break
        lines.append(line)
        shown += 1
    if shown < len(pairs):
        lines.append(f"…외 {len(pairs) - shown}건")
    embed.description = "\n".join(lines) or "경매를 기다리는 아이템이 없어요."
    pickable = sum(1 for _, o in pairs if not o.get("running"))
    tail = " (선택 목록은 25건까지)" if pickable > 25 else ""
    embed.set_footer(text=f"{len(pairs)}건 · 낙찰되면 목록에서 빠져요 · 아래에서 고르면 경매 생성 (관리자){tail}")
    return embed


def lot_label(lot):
    return f"{lot['item']} ({lot['unit']})" if lot.get("unit") else lot["item"]


def board_current(board):
    idx = board.get("current")
    lots = board.get("lots") or []
    if idx is None or not 0 <= idx < len(lots):
        return None
    return lots[idx]


def board_start_next(board, start_ts):
    """다음 대기 lot 을 start_ts 에 시작하도록 활성화. 남은 lot 이 없으면 진행판 종료. 반환: lot 또는 None."""
    for i, lot in enumerate(board.get("lots") or []):
        if lot.get("status") == "pending":
            lot["status"] = "active"
            lot["started_ts"] = float(start_ts)
            board["current"] = i
            return lot
    board["current"] = None
    board["status"] = "closed"
    board["closed_at"] = datetime.now().isoformat(timespec="seconds")
    return None


def board_state(board):
    """'closed' 종료 / 'active' 아이템 경매 중 / 'waiting' 관리자 ▶️ 대기."""
    if not board or board.get("status") == "closed":
        return "closed"
    lot = board_current(board)
    return "active" if lot and lot.get("status") == "active" else "waiting"


def board_after_close(board):
    """아이템 마감 뒤 처리 — 남은 대기 아이템이 없으면 진행판 종료. 반환: 종료 여부."""
    board["current"] = None
    if any(x.get("status") == "pending" for x in board.get("lots") or []):
        return False
    board["status"] = "closed"
    board["closed_at"] = datetime.now().isoformat(timespec="seconds")
    return True


def board_close_current(board, by=None):
    """현재 lot 낙찰/유찰 처리. 반환: 마감된 lot 또는 None."""
    lot = board_current(board)
    if not lot or lot.get("status") != "active":
        return None
    lot["status"] = "closed"
    lot["closed_at"] = datetime.now().isoformat(timespec="seconds")
    if by:
        lot["closed_by"] = by
    return lot


def board_unsold_lots(board):
    """끝난 진행판에서 낙찰되지 않은 아이템 (유찰 + 종료로 취소된 것)."""
    return [lot for lot in (board or {}).get("lots") or []
            if lot.get("status") == "cancelled" or (lot.get("status") == "closed" and not lot.get("bidder_id"))]


def board_can_relist(board):
    """🔁 유찰 재경매 버튼 활성 여부 — 진행판이 끝났고 유찰 아이템이 있을 때."""
    return bool(board) and board.get("status") == "closed" and bool(board_unsold_lots(board))


def relist_options(board, boards):
    """🔁 재경매 목록 — 이 진행판의 유찰 아이템 중 그 뒤에 다시 경매 중이거나 낙찰되지 않은 것.
    boards: 같은 보스탐의 진행판들(등록 순). 반환: [{item, unit, label, note, lot}]."""
    latest = latest_lots(boards)
    out = []
    for lot in board_unsold_lots(board):
        label = lot_label(lot)
        cur = latest.get(label, lot)
        st = cur.get("status")
        if st in ("pending", "active") or (st == "closed" and cur.get("bidder_id")):
            continue
        out.append({"item": lot["item"], "unit": lot.get("unit", ""), "label": label, "lot": lot,
                    "note": f"유찰 — 지난 시작 {fmt_amount_kr(lot['start_price'])} / 호가 {fmt_amount_kr(lot['increment'])}"})
    return out


class LotCreateModal(ui.Modal):
    """아이템 하나를 목록에서 고르고 시작가·호가·카운트를 칸마다 입력 → 경매장에 경매 메시지.
    🔨 경매생성(보스 알림)과 🔁 유찰 재경매(끝난 진행판)가 같이 쓴다.
    recheck: async () → 지금도 고를 수 있는 label 집합 (팝업을 연 사이 다른 사람이 올렸는지 확인)."""

    def __init__(self, auction, *, title, options, defaults, boss_name, boss_time, source_message_id, recheck):
        super().__init__(title=title)
        self.auction = auction
        self.options = options[:25]
        self.boss_name = boss_name
        self.boss_time = boss_time
        self.source_message_id = source_message_id
        self.recheck = recheck
        self.item_select = ui.Select(
            placeholder="경매할 아이템을 고르세요",
            options=[discord.SelectOption(label=o["label"][:100], value=str(i), description=o["note"][:100])
                     for i, o in enumerate(self.options)])
        self.start_input = ui.TextInput(default=fmt_amount_kr(defaults["start_price"]),
                                        placeholder="예: 100만, 1.5억, 3,750,000", max_length=30)
        self.inc_input = ui.TextInput(default=fmt_amount_kr(defaults["increment"]), placeholder="예: 10만", max_length=30)
        self.cd_input = ui.TextInput(default=str(int(defaults["countdown_sec"])), placeholder="예: 5", max_length=6)
        self.add_item(ui.Label(text="아이템", component=self.item_select))
        self.add_item(ui.Label(text="시작가", component=self.start_input))
        self.add_item(ui.Label(text="호가", description="입찰할 때마다 오르는 금액", component=self.inc_input))
        self.add_item(ui.Label(text="카운트 (초)", description="이 시간 동안 입찰이 없으면 마감", component=self.cd_input))

    async def on_submit(self, interaction: discord.Interaction):
        start = parse_amount_kr(self.start_input.value)
        inc = parse_amount_kr(self.inc_input.value)
        cd_text = self.cd_input.value.strip().replace("초", "")
        cd = int(cd_text) if cd_text.isdigit() else 0
        err = None
        if not self.item_select.values:
            err = "❌ 경매할 아이템을 골라 주세요."
        elif not start or not inc or start <= 0 or inc <= 0:
            err = f"❌ 시작가·호가를 확인해 주세요. {AMOUNT_HINT}"
        elif not 1 <= cd <= 3600:
            err = "❌ 카운트는 1~3600초 사이 숫자로 입력해 주세요."
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        pick = self.options[int(self.item_select.values[0])]
        await interaction.response.defer(ephemeral=True)
        if pick["label"] not in await self.recheck():
            await interaction.followup.send(f"⚠️ **{pick['label']}** 은(는) 이미 경매 중이거나 낙찰됐어요.",
                                            ephemeral=True)
            return
        _, text, _ = await self.auction.create_board(
            [make_lot(pick["item"], pick["unit"], start, inc, cd)], boss_name=self.boss_name,
            boss_time=self.boss_time, source_message_id=self.source_message_id, creator=interaction.user)
        await interaction.followup.send(text, ephemeral=True)


def merge_board_lots(boards):
    """같은 보스탐의 진행판들(등록 순)을 아이템별 최신 결과로 합친다. 순서는 처음 나온 순."""
    merged = {}
    for board in boards:
        for lot in board.get("lots") or []:
            if lot.get("status") in ("closed", "cancelled"):
                merged[lot_label(lot)] = lot
    return list(merged.values())


def board_results_text(board, currency=""):
    """보스 알림에 남길 한 줄 요약: '집행검 홍길동 130만 · 반지 (1/2) 유찰'."""
    return lots_results_text(board.get("lots") or [])


def lots_results_text(lots):
    parts = []
    for lot in lots:
        st = lot.get("status")
        if st == "closed" and lot.get("bidder_id"):
            parts.append(f"{lot_label(lot)} {lot.get('bidder_name')} {fmt_amount_kr(lot['current_price'])}")
        elif st == "closed":
            parts.append(f"{lot_label(lot)} 유찰")
        elif st == "cancelled":
            parts.append(f"{lot_label(lot)} 취소")
    return " · ".join(parts)


def build_board_embed(board, currency="", now=None):
    unit = f" {currency}" if currency else ""
    now = time.time() if now is None else now
    lots = board.get("lots") or []
    closed = board.get("status") == "closed"
    head = f"{board.get('boss_name', '')} {board.get('boss_time', '')}".strip()
    embed = discord.Embed(
        title=f"🏁 경매 완료 — {head}" if closed else (f"🔨 경매 진행판 — {head}" if len(lots) > 1 else f"🔨 경매 — {head}"),
        color=0x1d9e75 if closed else 0xf0b232)
    lines = []
    for lot in lots:
        st = lot.get("status")
        label = lot_label(lot)
        if st == "closed" and lot.get("bidder_id"):
            lines.append(f"✅ **{label}** — {lot.get('bidder_name')} **{fmt_amount_kr(lot['current_price'])}**")
        elif st == "closed":
            lines.append(f"⚪ {label} — 유찰")
        elif st == "cancelled":
            lines.append(f"⛔ {label} — 취소")
        elif st == "active":
            lines.append(f"▶️ **{label}** — 진행 중")
        else:
            lines.append(f"⏳ {label} · 시작 {fmt_amount_kr(lot['start_price'])} / 호가 {fmt_amount_kr(lot['increment'])}")
    desc = "\n".join(lines) or "(아이템 없음)"
    if len(desc) > 3900:
        desc = desc[:3900] + "\n…"
    embed.description = desc
    lot = board_current(board)
    state = board_state(board)
    if state == "waiting":
        nxt = next((x for x in lots if x.get("status") == "pending"), None)
        if nxt:
            embed.add_field(name="⏸️ 다음 경매", value=(
                f"**{lot_label(nxt)}** · 시작 {int(nxt['start_price']):,}{unit} / 호가 {int(nxt['increment']):,}{unit}"
                f" / {nxt['countdown_sec']}초\n관리자가 ▶️ 경매시작을 누르면 시작돼요."), inline=False)
    if lot and state == "active":
        embed.add_field(name="🔨 지금 경매", value=f"**{lot_label(lot)}**", inline=False)
        embed.add_field(name="시작가", value=f"{int(lot['start_price']):,}{unit}", inline=True)
        embed.add_field(name="호가", value=f"{int(lot['increment']):,}{unit}", inline=True)
        embed.add_field(name="카운트", value=f"{lot['countdown_sec']}초", inline=True)
        embed.description = (desc + "\n\n" + live_block(lot, unit, now))[:4090]
        hist = bid_history_text(lot)
        if hist:
            embed.add_field(name=f"📜 최근 입찰 (총 {lot.get('bid_count', 0)}회)", value=hist, inline=False)
        if board.get("source_message_id"):
            embed.add_field(name="🔐 입찰 자격", value="해당 보스탐 참석자만 입찰할 수 있어요.", inline=False)
    done = sum(1 for x in lots if x.get("status") in ("closed", "cancelled"))
    tail = {"active": "🙋 입찰을 누르면 호가만큼 올라갑니다", "waiting": "▶️ 관리자 시작 대기"}.get(state, "모든 경매가 끝났어요")
    embed.set_footer(text=f"{done}/{len(lots)} 완료 · {tail} · 등록자 {board.get('creator_name', '-')}")
    return embed


class BoardView(ui.View):
    """경매 진행판 버튼 (persistent): ▶️ 경매시작 (관리자, 아이템마다) / 🙋 입찰 / ⏹️ 종료 (관리자)
    / 🔁 유찰 재경매 (관리자, 끝난 뒤 유찰 아이템만 새 진행판으로)."""

    def __init__(self, auction: BossAuction, state="waiting", relist=False, locked=False):
        super().__init__(timeout=None)
        self.auction = auction
        for child in self.children:
            cid = getattr(child, "custom_id", "")
            if cid == "boss_board_start":
                child.disabled = state != "waiting"
            elif cid == "boss_board_bid":
                child.disabled = state != "active" or locked
                if locked and state == "active":
                    child.label, child.emoji = "잠시 대기", "⏳"
            elif cid == "boss_board_stop":
                child.disabled = state == "closed"
            elif cid == "boss_board_relist":
                child.disabled = not relist

    @ui.button(label="경매시작", emoji="▶️", style=discord.ButtonStyle.primary, custom_id="boss_board_start")
    async def start_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        if not a.is_admin(interaction.user):
            await interaction.response.send_message("⛔ 관리자만 경매를 시작할 수 있어요.", ephemeral=True)
            return
        mid = interaction.message.id
        clicked = a.click_ts(interaction)
        pre = a.store.get(mid) or {}
        # 시작 시점 참석자로 입찰 자격 갱신 (등록 후 인원체크로 추가된 참석자 반영)
        allowed = await a.load_allowed(pre.get("source_message_id"))
        lot, err = await a.start_next_lot(mid, clicked, allowed)
        if lot is None:
            await interaction.response.send_message(err or "남은 아이템이 없어요.", ephemeral=True)
            return
        board = a.store.get(mid)
        await interaction.response.edit_message(embed=build_board_embed(board, a.currency, a.discord_now()),
                                                view=a.view_for(board))
        a.log.info(f"[진행판 시작] {lot_label(lot)} | by {interaction.user.display_name} | msg {mid}")

    @ui.button(label="입찰", emoji="🙋", style=discord.ButtonStyle.success, custom_id="boss_board_bid")
    async def bid_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        mid = interaction.message.id
        clicked = a.click_ts(interaction)   # 판정 기준 = 누른 순간
        # 잠금 안에서는 판정·저장만 하고 Discord 응답은 밖에서 — 연타가 몰려도 뒤 클릭이 3초 응답 시한을 넘기지 않도록
        reject, label, price, count = None, "", 0, 0
        async with a.lock:
            board = a.store.get(mid)
            lot = board_current(board) if board else None
            if not lot or board.get("status") != "active" or lot.get("status") != "active":
                reject = "경매가 끝났어요."
            else:
                started = float(lot.get("started_ts") or 0)
                dl = close_deadline(lot)
                if clicked < started:
                    reject = f"⏳ **{lot_label(lot)}** 경매는 <t:{int(started) + 1}:R> 시작해요."
                elif dl is not None and clicked >= dl:
                    reject = "⏰ 카운트가 끝난 뒤 눌렀어요. 마감 처리됩니다."
                elif (until := bid_lock_until(lot)) and clicked < until and str(lot.get("bidder_id")) != str(interaction.user.id):
                    reject = (f"⏳ 방금 **{lot.get('bidder_name')}**님 입찰이 먼저 들어왔어요. "
                              f"{BID_SETTLE_SEC}초 뒤 카운트가 시작되면 다시 눌러 주세요.")
                elif not a.can_bid(board, interaction.user.id):
                    reject = "🔐 이 경매는 해당 보스탐 참석자만 입찰할 수 있어요."
                else:
                    ok, text = apply_bid(lot, interaction.user.id, interaction.user.display_name, now=clicked)
                    if not ok:
                        reject = text
                    else:
                        a.safe_save(mid, board)
                        a._schedule_board(mid, board)
                        label, price, count = lot_label(lot), int(lot["current_price"]), lot["bid_count"]
        if reject:
            await a.respond_ephemeral(interaction, reject)
            return
        a.log.info(f"[진행판 입찰] {label} | {interaction.user.display_name}({interaction.user.id}) "
                   f"{price:,} | {count}회 | msg {mid}")
        latest = a.store.get(mid)   # 응답 순서가 뒤바뀌어도 항상 최신 상태로 그린다
        if latest:
            await a.respond_edit(interaction, embed=build_board_embed(latest, a.currency, a.discord_now()),
                                 view=a.view_for(latest))
        a.schedule_refresh(interaction.message)   # 연타 뒤 최종 입찰자로 확정 표시

    @ui.button(label="종료", emoji="⏹️", style=discord.ButtonStyle.secondary, custom_id="boss_board_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        if not a.is_admin(interaction.user):
            await interaction.response.send_message("⛔ 관리자만 경매를 종료할 수 있어요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if board_state(a.store.get(interaction.message.id)) == "closed":
            await interaction.followup.send("이미 끝난 경매예요.", ephemeral=True)
            return
        await a.advance_board(interaction.message.id, by=interaction.user.display_name, stop=True)
        text = "⏹️ 진행판을 종료했어요. 진행 중이던 아이템은 마감, 남은 아이템은 취소됐어요."
        await interaction.followup.send(text, ephemeral=True)

    @ui.button(label="유찰 재경매", emoji="🔁", style=discord.ButtonStyle.primary, custom_id="boss_board_relist")
    async def relist_btn(self, interaction: discord.Interaction, button: ui.Button):
        a = self.auction
        if not a.is_admin(interaction.user):
            await interaction.response.send_message("⛔ 관리자만 재경매를 올릴 수 있어요.", ephemeral=True)
            return
        mid = interaction.message.id
        board = a.store.get(mid)
        src = (board or {}).get("source_message_id")

        def _options():
            fresh = a.store.get(mid) or board
            return relist_options(fresh, a.boards_for_source(src) if src else [fresh])

        options = _options() if board_can_relist(board) else []
        if not options:
            await interaction.response.send_message("다시 올릴 유찰 아이템이 없어요 (이미 재경매 중이거나 낙찰됨).",
                                                    ephemeral=True)
            return

        async def _recheck():
            return {o["label"] for o in _options()}

        first = options[0]["lot"]
        await interaction.response.send_modal(LotCreateModal(
            a, title="🔁 유찰 재경매", options=options,
            defaults={"start_price": first["start_price"], "increment": first["increment"],
                      "countdown_sec": first["countdown_sec"]},
            boss_name=board.get("boss_name", ""), boss_time=board.get("boss_time", ""),
            source_message_id=src, recheck=_recheck))
