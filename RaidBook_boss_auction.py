# -*- coding: utf-8 -*-
"""
RaidBook_boss_auction.py
보스봇 전리품 경매 (카운트다운 방식, 포인트 연동 없음).

흐름
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
    rec["last_bid_at"] = float(now if now is not None else time.time())
    rec.setdefault("bid_history", []).append({"uid": str(uid), "name": name, "price": price,
                                              "at": datetime.now().isoformat(timespec="seconds")})
    return True, f"🙋 {price:,} 입찰했어요."


def close_deadline(rec):
    """마감 예정 unix 시각 = (마지막 입찰 또는 시작 시각) + 카운트. 진행 전이면 None."""
    base = rec.get("last_bid_at") or rec.get("started_ts")
    if rec.get("status") != "active" or not base:
        return None
    return float(base) + int(rec["countdown_sec"])


class AuctionStore:
    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger
        self._data = None

    def load(self):
        if self._data is None:
            data = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
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
            embed = discord.Embed(
                title=f"🔨 경매 진행중 — {item}",
                description=f"🙋 입찰을 누르면 금액이 호가만큼 올라갑니다.\n"
                            f"**{rec.get('countdown_sec')}초** 동안 새 입찰이 없으면 마감!",
                color=0xf0b232)
        embed.add_field(name="시작가", value=f"{int(rec.get('start_price', 0)):,}{unit}", inline=True)
        embed.add_field(name="호가", value=f"{int(rec.get('increment', 0)):,}{unit}", inline=True)
        embed.add_field(name="카운트", value=f"{rec.get('countdown_sec')}초", inline=True)
        if bidder:
            embed.add_field(name="최종 입찰자", value=f"**{bidder}**", inline=True)
            embed.add_field(name="현재가", value=f"**{price:,}{unit}**", inline=True)
        elif status == "active":
            embed.add_field(name="현재가", value="아직 입찰 없음", inline=False)
        dl = close_deadline(rec)
        if dl and status == "active":
            embed.add_field(name="⏱️ 마감", value=f"<t:{int(dl) + 1}:R>", inline=False)
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
        self.clock_offset = 0.0   # PC 시계 − Discord 시계 (초)

    def click_ts(self, interaction):
        """버튼을 누른 순간의 Discord 시각(unix). PC 시계 차이도 함께 갱신."""
        ts = interaction.created_at.timestamp()
        # 처리 지연은 항상 양수 → 관측값 중 최소가 실제 시계 차이에 가장 가깝다 (최근 값으로 서서히 갱신)
        observed = time.time() - ts
        self.clock_offset = observed if abs(observed - self.clock_offset) > 2 else min(observed, self.clock_offset + 0.05)
        return ts

    def discord_now(self):
        return time.time() - self.clock_offset

    def view_for(self, rec):
        return AuctionView(self, state=(rec or {}).get("status", "closed"))

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

    async def restore(self):
        """재시작 후 진행 중 경매의 마감 타이머 재등록 (마감 지난 건 즉시 낙찰)."""
        for mid, rec in self.store.load().items():
            if rec.get("status") == "active":
                self._schedule_close(mid, rec)
                self.log.info(f"[경매 복구] {rec.get('item')} | msg {mid}")


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

    def __init__(self, auction: BossAuction, state="ready"):
        super().__init__(timeout=None)
        self.auction = auction
        for child in self.children:
            cid = getattr(child, "custom_id", "")
            if cid == "boss_auc_start":
                child.disabled = state != "ready"
            elif cid == "boss_auc_bid":
                child.disabled = state != "active"
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
        async with a.lock:
            rec = a.store.get(mid)
            if not rec:
                await interaction.response.send_message("경매 정보를 찾을 수 없어요.", ephemeral=True)
                return
            dl = close_deadline(rec)
            if dl is not None and clicked >= dl:
                await interaction.response.send_message("⏰ 카운트가 끝난 뒤 눌렀어요. 마감 처리됩니다.", ephemeral=True)
                return
            if rec.get("status") == "active" and not a.can_bid(rec, interaction.user.id):
                await interaction.response.send_message(
                    "🔐 이 경매는 해당 보스탐 참석자만 입찰할 수 있어요.", ephemeral=True)
                return
            ok, text = apply_bid(rec, interaction.user.id, interaction.user.display_name, now=clicked)
            if not ok:
                await interaction.response.send_message(text, ephemeral=True)
                return
            a.store.save(mid, rec)
            await interaction.response.edit_message(embed=build_embed(rec, a.currency), view=a.view_for(rec))
            a._schedule_close(mid, rec)
        a.log.info(f"[경매 입찰] {rec.get('item')} | {interaction.user.display_name}({interaction.user.id}) "
                   f"{int(rec['current_price']):,} | {rec['bid_count']}회 | msg {mid}")

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
