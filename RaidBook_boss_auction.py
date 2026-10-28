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
        # 경매 진행판 전체 종료 콜백 async (board) -> None (보스 알림에 결과 반영)
        self.on_board_finished = None
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

    def view_for(self, rec):
        if (rec or {}).get("type") == "board":
            return BoardView(self, state=board_state(rec), relist=board_can_relist(rec))
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

    def boards_for_source(self, source_message_id):
        """같은 보스 알림에서 만든 진행판 전부 (등록 순). 재경매 결과 합치기용."""
        boards = [rec for rec in self.store.load().values()
                  if rec.get("type") == "board" and str(rec.get("source_message_id")) == str(source_message_id)]
        return sorted(boards, key=lambda b: b.get("created_at") or "")

    async def relist_board(self, mid, creator, lots):
        """🔁 끝난 진행판의 유찰 아이템을 (재입력한 시작가·호가·카운트로) 새 진행판에 올린다. 반환: (ok, 안내문)."""
        async with self.lock:
            board = self.store.get(mid)
            if not board_can_relist(board):
                return False, "다시 올릴 유찰 아이템이 없거나 이미 재경매를 올렸어요."
            src = board.get("source_message_id")
            if src and self.find_open_board(src):
                return False, "⏳ 이 보스탐의 다른 경매가 아직 진행 중이에요. 끝난 뒤 눌러 주세요."
            board["relisted_to"] = "pending"   # 연타 방지 — 게시 실패 시 되돌림
            self.store.save(mid, board)
        ok, text, msg = await self.create_board(
            lots, boss_name=board.get("boss_name", ""), boss_time=board.get("boss_time", ""),
            source_message_id=board.get("source_message_id"), creator=creator)
        async with self.lock:
            board = self.store.get(mid) or board
            if ok and msg is not None:
                board["relisted_to"] = str(msg.id)
            else:
                board.pop("relisted_to", None)
            self.store.save(mid, board)
        try:
            ch = self.bot.get_channel(int(board["channel_id"])) or await self.bot.fetch_channel(int(board["channel_id"]))
            old = await ch.fetch_message(int(mid))
            await old.edit(view=self.view_for(board))
        except Exception as e:
            self.log.info(f"[재경매 원본 버튼 갱신 실패] {mid}: {type(e).__name__}: {e}")
        if ok:
            self.log.info(f"[진행판 재경매] {board.get('boss_name')} {board.get('boss_time')} | {len(lots)}건 "
                          f"| {mid} → {msg.id if msg else '-'} | by {creator.display_name}")
        return ok, text

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
    """🔁 유찰 재경매 가능 여부 — 진행판이 끝났고, 아직 재경매를 안 올렸고, 유찰 아이템이 있을 때."""
    return (bool(board) and board.get("status") == "closed" and not board.get("relisted_to")
            and bool(board_unsold_lots(board)))


def relist_text(board):
    """재경매 입력창 기본값 — 유찰 아이템 한 줄씩 '이름 (순번) 시작가 호가 N초'.
    순번을 이름에 남겨 두면 결과를 합칠 때 같은 아이템 결과를 겹쳐 쓴다."""
    return "\n".join(f"{lot_label(lot)} {fmt_amount_kr(lot['start_price'])} {fmt_amount_kr(lot['increment'])} "
                     f"{int(lot['countdown_sec'])}초" for lot in board_unsold_lots(board))


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
        title=f"🏁 경매 완료 — {head}" if closed else f"🔨 경매 진행판 — {head}",
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
        if lot.get("bidder_id"):
            embed.add_field(name="최고 입찰자", value=f"**{lot['bidder_name']}**", inline=True)
            embed.add_field(name="현재가", value=f"**{int(lot['current_price']):,}{unit}**", inline=True)
        else:
            embed.add_field(name="현재가", value="아직 입찰 없음", inline=True)
        started = float(lot.get("started_ts") or 0)
        if started > now:
            embed.add_field(name="⏳ 시작", value=f"<t:{int(started) + 1}:R>", inline=False)
        else:
            dl = close_deadline(lot)
            if dl:
                embed.add_field(name="⏱️ 마감", value=f"<t:{int(dl) + 1}:R>", inline=False)
        if board.get("source_message_id"):
            embed.add_field(name="🔐 입찰 자격", value="해당 보스탐 참석자만 입찰할 수 있어요.", inline=False)
    done = sum(1 for x in lots if x.get("status") in ("closed", "cancelled"))
    tail = {"active": "🙋 입찰을 누르면 호가만큼 올라갑니다", "waiting": "▶️ 관리자 시작 대기"}.get(state, "모든 경매가 끝났어요")
    embed.set_footer(text=f"{done}/{len(lots)} 완료 · {tail} · 등록자 {board.get('creator_name', '-')}")
    return embed


class BoardView(ui.View):
    """경매 진행판 버튼 (persistent): ▶️ 경매시작 (관리자, 아이템마다) / 🙋 입찰 / ⏹️ 종료 (관리자)
    / 🔁 유찰 재경매 (관리자, 끝난 뒤 유찰 아이템만 새 진행판으로)."""

    def __init__(self, auction: BossAuction, state="waiting", relist=False):
        super().__init__(timeout=None)
        self.auction = auction
        for child in self.children:
            cid = getattr(child, "custom_id", "")
            if cid == "boss_board_start":
                child.disabled = state != "waiting"
            elif cid == "boss_board_bid":
                child.disabled = state != "active"
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
        async with a.lock:
            board = a.store.get(mid)
            lot = board_current(board) if board else None
            if not lot or board.get("status") != "active":
                await interaction.response.send_message("경매가 끝났어요.", ephemeral=True)
                return
            started = float(lot.get("started_ts") or 0)
            if clicked < started:
                await interaction.response.send_message(
                    f"⏳ **{lot_label(lot)}** 경매는 <t:{int(started) + 1}:R> 시작해요.", ephemeral=True)
                return
            dl = close_deadline(lot)
            if dl is not None and clicked >= dl:
                await interaction.response.send_message("⏰ 카운트가 끝난 뒤 눌렀어요. 마감 처리됩니다.", ephemeral=True)
                return
            if not a.can_bid(board, interaction.user.id):
                await interaction.response.send_message(
                    "🔐 이 경매는 해당 보스탐 참석자만 입찰할 수 있어요.", ephemeral=True)
                return
            ok, text = apply_bid(lot, interaction.user.id, interaction.user.display_name, now=clicked)
            if not ok:
                await interaction.response.send_message(text, ephemeral=True)
                return
            a.store.save(mid, board)
            await interaction.response.edit_message(embed=build_board_embed(board, a.currency, a.discord_now()),
                                                    view=a.view_for(board))
            a._schedule_board(mid, board)
        a.log.info(f"[진행판 입찰] {lot_label(lot)} | {interaction.user.display_name}({interaction.user.id}) "
                   f"{int(lot['current_price']):,} | {lot['bid_count']}회 | msg {mid}")

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
        board = a.store.get(interaction.message.id)
        if not board_can_relist(board):
            await interaction.response.send_message("다시 올릴 유찰 아이템이 없거나 이미 재경매를 올렸어요.", ephemeral=True)
            return
        await interaction.response.send_modal(RelistModal(a, interaction.message.id, relist_text(board)))


class RelistModal(ui.Modal, title="🔁 유찰 재경매"):
    """유찰 아이템의 시작가·호가·카운트를 새로 입력. 줄을 지우면 그 아이템은 다시 올리지 않는다."""

    def __init__(self, auction: BossAuction, board_mid, prefill):
        super().__init__()
        self.auction = auction
        self.board_mid = board_mid
        self.lots_input = ui.TextInput(
            label="재경매 아이템 (이름 시작가 호가 N초)",
            style=discord.TextStyle.paragraph, max_length=1500, default=prefill[:1500],
            placeholder="반지 (2/2) 20만 2만 5초\n숫자를 고치고, 안 올릴 줄은 지우세요",
        )
        self.add_item(self.lots_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            lots, _ = parse_board_lots(self.lots_input.value, self.auction.board_defaults)
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}\n입력한 내용 (복사해서 다시 쓰세요):\n```\n{self.lots_input.value[:1500]}\n```",
                ephemeral=True)
            return
        if not lots:
            await interaction.response.send_message("❌ 다시 올릴 아이템을 한 줄 이상 남겨 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _, text = await self.auction.relist_board(self.board_mid, interaction.user, lots)
        await interaction.followup.send(text, ephemeral=True)
