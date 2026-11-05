# -*- coding: utf-8 -*-
"""
혈맹별 분배 정산 (보탐 경매건 단위) — 공용 로직.

한 보탐 알림에서 낙찰된 아이템 금액을 모두 더해 (유찰 재경매로 나중에 팔린 것 포함)
  1인당 = 합계 ÷ 전체 참석자 수 (원 단위 절사)
  혈맹별 = 1인당 × 그 혈맹 참석자 수
나누어떨어지지 않는 나머지는 잔액으로 따로 둔다.
누를 때마다 다시 계산하고 최신 결과를 Data/clan_settlements.json(보탐 알림ID별)에 남긴다.
"""
import json
import os
from datetime import datetime

from RaidBook_common import write_json_atomic

UNASSIGNED = "미지정"
UNASSIGNED_EMOJI = "❔"
ALL_CLAN = "전체"          # 혈맹 구분을 끈 서버 — 한 묶음으로
ALL_EMOJI = "👥"
STAFF_SUFFIX = "운영진"


def won(n):
    return f"{int(n):,}"


def compute(sold_lots, attendee_count, clan_groups):
    """sold_lots: [{label, winner_name, price}], clan_groups: [(혈맹명, 이모지, [참석자 이름…])] (순서 유지).
    반환: total / attendee_count / per / clans [{name, emoji, count, amount, members}] / remainder.
    참석자가 없으면 ValueError."""
    total = sum(int(x.get("price") or 0) for x in sold_lots)
    n = int(attendee_count)
    if n <= 0:
        raise ValueError("이 보탐의 참석자가 없어 나눌 수 없어요.")
    per = total // n
    clans = [{"name": name, "emoji": emoji, "count": len(people), "amount": per * len(people),
              "members": list(people)} for name, emoji, people in clan_groups]
    remainder = total - sum(c["amount"] for c in clans)
    return {"total": total, "attendee_count": n, "per": per, "clans": clans, "remainder": remainder}


def clan_groups_from_split(split, attendee_labels):
    """RaidBook_clans.group_by_clan 결과 (혈맹 그룹, 미지정) → [(혈맹명, 이모지, [이름…])].
    split이 None(혈맹 구분 끔)이면 전체 한 묶음. 미지정은 있을 때만 넣는다."""
    if split is None:
        return [(ALL_CLAN, ALL_EMOJI, list(attendee_labels))]
    groups, unassigned = split
    out = [(name, emoji, list(people)) for name, emoji, people in groups]
    if unassigned:
        out.append((UNASSIGNED, UNASSIGNED_EMOJI, list(unassigned)))
    return out


def viewer_clan(role_names, clan_names):
    """누른 사람의 혈맹 — '슈퍼맨운영진'처럼 '<혈맹>운영진' 역할이 먼저, 없으면 혈맹 역할 (설정 순서상 첫 번째)."""
    roles = [str(r).strip() for r in role_names or []]
    for clan in clan_names:
        if f"{clan}{STAFF_SUFFIX}" in roles:
            return clan
    for clan in clan_names:
        if clan in roles:
            return clan
    return None


def title_of(rec):
    """'11/07 21:00 오만의탑 정상' — 날짜는 알림 메시지 시각에서."""
    return " ".join(x for x in (rec.get("boss_date"), rec.get("boss_time"), rec.get("boss_name")) if x)


def detail_text(rec, currency="", viewer=None, short=None):
    """💰 정산 결과 (누른 사람만 보임): 낙찰 내역 · 1인당 · 혈맹별 금액 · 누른 운영진 혈맹의 혈원 명단."""
    unit = f" {currency}" if currency else ""
    short = short or (lambda s: s)
    lots = rec.get("lots") or []
    lines = [f"💰 **정산** — {title_of(rec)}",
             f"낙찰 {len(lots)}건 · 합계 **{won(rec.get('total', 0))}{unit}**"]
    for x in lots:
        lines.append(f"· {x['label']} — {x.get('winner_name', '')} {won(x.get('price', 0))}")
    unsold = rec.get("unsold") or []
    if unsold:
        lines.append(f"⚪ 미낙찰 {len(unsold)}건 ({', '.join(unsold)[:200]}) — 재경매로 낙찰되면 다시 눌러 갱신")
    lines.append("")
    lines.append(f"참석 {rec.get('attendee_count', 0)}명 → 1인당 **{won(rec.get('per', 0))}**")
    for c in rec.get("clans") or []:
        lines.append(f"{c['emoji']} {c['name']} {c['count']}명 → **{won(c['amount'])}**")
    if rec.get("remainder"):
        lines.append(f"잔액 {won(rec['remainder'])} (나누어떨어지지 않는 금액)")
    mine = next((c for c in rec.get("clans") or [] if viewer and c["name"] == viewer), None)
    if mine and mine["members"]:
        lines.append("")
        lines.append(f"{mine['emoji']} **{mine['name']} 혈원** {mine['count']}명 · 1인당 {won(rec.get('per', 0))}")
        lines.append(" · ".join(short(m) for m in mine["members"]))
    lines.append(f"-# {rec.get('updated_at', '')[5:16].replace('T', ' ')} 기준 · 누를 때마다 최신 낙찰금으로 다시 계산")
    return "\n".join(lines)[:1990]


class ClanSettleStore:
    """Data/clan_settlements.json — {보탐 알림ID: 마지막 정산 결과}."""

    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger

    def load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, mid):
        return self.load().get(str(mid))

    def save(self, mid, rec):
        data = self.load()
        old = data.get(str(mid)) or {}
        rec = dict(rec)
        now = datetime.now().isoformat(timespec="seconds")
        rec["created_at"] = old.get("created_at") or now
        rec["updated_at"] = now
        data[str(mid)] = rec
        write_json_atomic(self.path, data, self.log)
        return rec
