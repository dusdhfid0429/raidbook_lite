# -*- coding: utf-8 -*-
import json
import math
import os
from typing import List, Dict, Tuple

import RaidBook_members as rb_members

ATTR_SKILL_MAP = {
    "땅": "블레스오브어스",
    "불": "블레스오브파이어",
    "바람": "아이오브스톰",
}


def load_party_config(base_dir: str) -> dict:
    path = os.path.join(base_dir, "Config", "party_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_party_params(base_dir: str) -> tuple[int, int]:
    cfg = load_party_config(base_dir)
    return cfg.get("max_party_size", 8), cfg.get("min_single_party", 6)


def _load_profiles(base_dir: str) -> dict:
    return rb_members.load_profiles(base_dir)


def _load_id_to_name(base_dir: str) -> dict:
    path = os.path.join(base_dir, "Data", "user_mapping.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _calc_power(profile: dict) -> int:
    return rb_members.calc_power(profile)   # 전투력 산식 단일 정의 (RaidBook_members)


def _build_member(uid: str, profile: dict, id_to_name: dict) -> dict:
    return {
        "uid": uid,
        "name": id_to_name.get(uid, uid),
        "class": profile.get("class", "미상"),
        "level": profile.get("level", 0),
        "weapon_enh": profile.get("weapon_enh", 0),
        "armor_enh": profile.get("armor_enh", 0),
        "power": _calc_power(profile),
        "fairy_attr": profile.get("fairy_attr", ""),
        "fairy_attr_skill": bool(profile.get("fairy_attr_skill", False)),
        "special_skill": profile.get("special_skill", ""),
    }


def compose_parties(
    attendee_uids: List[str],
    base_dir: str,
) -> Tuple[List[List[dict]], List[str]]:
    """
    Returns (parties, warnings).
    parties: list of list of member dict (each dict has uid/name/class/level/weapon_enh/armor_enh/power/fairy_attr/fairy_attr_skill/special_skill)
    warnings: list of Korean warning strings
    """
    profiles = _load_profiles(base_dir)
    id_to_name = _load_id_to_name(base_dir)

    max_party_size, min_single_party = _get_party_params(base_dir)

    n = len(attendee_uids)
    if n == 0:
        return [], []

    if n <= min_single_party:
        num_parties = 1
    else:
        num_parties = math.ceil(n / max_party_size)

    # Distribute party sizes (larger parties first)
    base_size = n // num_parties
    extra = n % num_parties
    party_sizes = [base_size + (1 if i < extra else 0) for i in range(num_parties)]

    members = [_build_member(uid, profiles.get(uid, {}), id_to_name) for uid in attendee_uids]

    warnings: List[str] = []
    parties: List[List[dict]] = [[] for _ in range(num_parties)]
    assigned: set = set()

    def assign(member, idx):
        parties[idx].append(member)
        assigned.add(member["uid"])

    def available_slots(idx):
        return party_sizes[idx] - len(parties[idx])

    # ── Step 0: 파티장 (기사, 전투력 순) ──────────────────────────────
    knights_sorted = sorted(
        [m for m in members if m["class"] == "기사"],
        key=lambda x: (0 if x["special_skill"] else 1, -x["power"]),
    )
    for i in range(num_parties):
        if i < len(knights_sorted):
            assign(knights_sorted[i], i)
        else:
            # 기사 부족 → 전투력 최상위 미배정 멤버로 대체
            fallback = sorted(
                [m for m in members if m["uid"] not in assigned], key=lambda x: -x["power"]
            )
            if fallback:
                assign(fallback[0], i)
            warnings.append(f"파티 {i + 1}에 기사 파티장 없음 (대체 배정)")

    # ── Step 1: 특수 스킬 그룹화 + 특수 파티 크기 8 고정 ─────────────
    special_holders = [m for m in members if m["uid"] not in assigned and m["special_skill"]]
    special_party_indices: set = set()
    if special_holders:
        target_party = max(range(num_parties), key=lambda i: available_slots(i))
        for m in special_holders:
            if available_slots(target_party) > 0:
                assign(m, target_party)
                special_party_indices.add(target_party)
            else:
                alt = max(
                    [i for i in range(num_parties) if available_slots(i) > 0],
                    key=lambda i: available_slots(i),
                    default=None,
                )
                if alt is not None:
                    assign(m, alt)
                    special_party_indices.add(alt)

        # 특수 파티 크기를 8로 고정 (전체 참석자 8명 이상일 때)
        if n >= max_party_size:
            for sp_idx in special_party_indices:
                current_size = party_sizes[sp_idx]
                if current_size < max_party_size:
                    delta = max_party_size - current_size
                    party_sizes[sp_idx] = max_party_size
                    # 늘린 슬롯만큼 다른 파티에서 균등하게 줄임
                    others = [i for i in range(num_parties) if i not in special_party_indices]
                    for _ in range(delta):
                        if not others:
                            break
                        # 가장 큰 파티에서 1씩 감소
                        biggest = max(others, key=lambda i: party_sizes[i])
                        if party_sizes[biggest] > 1:
                            party_sizes[biggest] -= 1

    # ── Step 2: 마법사 (특수 파티는 2명, 일반 파티는 1명, 전투력 순) ──
    mages_sorted = sorted(
        [m for m in members if m["class"] == "마법사" and m["uid"] not in assigned],
        key=lambda x: -x["power"],
    )
    mage_idx = 0
    for i in range(num_parties):
        quota = 2 if i in special_party_indices else 1
        # 이미 배정된 마법사(특수스킬 보유 마법사 등) 수 반영
        assigned_count = sum(1 for m in parties[i] if m["class"] == "마법사")
        while assigned_count < quota and mage_idx < len(mages_sorted):
            if available_slots(i) > 0:
                assign(mages_sorted[mage_idx], i)
                mage_idx += 1
                assigned_count += 1
            else:
                break
        if assigned_count < quota:
            warnings.append(f"파티 {i + 1}에 마법사 {quota}명 필요 ({assigned_count}명만 배정)")


    # ── Step 3: 속성 스킬 요정 (파티별 최소 1명, 속성 다양성 우선) ──
    attr_fairies = sorted(
        [m for m in members if m["class"] == "요정" and m["fairy_attr_skill"] and m["uid"] not in assigned],
        key=lambda x: -x["power"],
    )

    def existing_attrs(idx):
        return {m["fairy_attr"] for m in parties[idx] if m.get("fairy_attr")}

    party_has_attr_fairy = [False] * num_parties
    for i in range(num_parties):
        if available_slots(i) <= 0:
            continue
        # 속성 겹치지 않는 요정 우선
        cur_attrs = existing_attrs(i)
        candidate = next(
            (m for m in attr_fairies if m["uid"] not in assigned and m["fairy_attr"] not in cur_attrs),
            next((m for m in attr_fairies if m["uid"] not in assigned), None),
        )
        if candidate:
            assign(candidate, i)
            party_has_attr_fairy[i] = True

    for i, has in enumerate(party_has_attr_fairy):
        if not has:
            warnings.append(f"파티 {i + 1}에 속성 스킬 요정 없음")

    # ── Step 4: 일반 요정 (요정 없는 파티 우선) ──────────────────────
    normal_fairies = sorted(
        [m for m in members if m["class"] == "요정" and not m["fairy_attr_skill"] and m["uid"] not in assigned],
        key=lambda x: -x["power"],
    )
    for i in range(num_parties):
        has_fairy = any(m["class"] == "요정" for m in parties[i])
        if not has_fairy and available_slots(i) > 0 and normal_fairies:
            candidate = next((m for m in normal_fairies if m["uid"] not in assigned), None)
            if candidate:
                assign(candidate, i)

    # ── Step 5: 근거리:요정 비율 (남은 자리 기준 1:1) ────────────────
    remaining_melee = sorted(
        [m for m in members if m["class"] in ("기사", "군주") and m["uid"] not in assigned],
        key=lambda x: -x["power"],
    )
    remaining_fairies = sorted(
        [m for m in members if m["class"] == "요정" and m["uid"] not in assigned],
        key=lambda x: -x["power"],
    )

    for i in range(num_parties):
        slots = available_slots(i)
        if slots <= 0:
            continue
        fairy_count = sum(1 for m in parties[i] if m["class"] == "요정")
        melee_count = sum(1 for m in parties[i] if m["class"] in ("기사", "군주"))
        # 기존 비율 보정 후 남은 슬롯을 1:1로 채움
        deficit_fairy = max(0, melee_count - fairy_count)
        fairy_slots = min(slots, (slots + deficit_fairy) // 2 + deficit_fairy % 2)
        melee_slots = slots - fairy_slots

        for _ in range(fairy_slots):
            avail = [m for m in remaining_fairies if m["uid"] not in assigned]
            if avail:
                assign(avail[0], i)
        for _ in range(melee_slots):
            avail = [m for m in remaining_melee if m["uid"] not in assigned]
            if avail:
                assign(avail[0], i)

    # ── Step 6: 나머지 분산 ───────────────────────────────────────────
    # 특수 파티 빈 슬롯은 고전투력 순으로 먼저 채운 뒤, 일반 파티는 전투력 균형 배분
    remaining_all = sorted(
        [m for m in members if m["uid"] not in assigned], key=lambda x: -x["power"]
    )
    # 특수 파티 우선 채우기 (고전투력 순)
    for m in list(remaining_all):
        if m["uid"] in assigned:
            continue
        eligible_special = [i for i in special_party_indices if available_slots(i) > 0]
        if eligible_special:
            target = max(eligible_special, key=lambda i: available_slots(i))
            assign(m, target)

    # 일반 파티 채우기 (전투력 낮은 파티 우선)
    remaining_all2 = sorted(
        [m for m in members if m["uid"] not in assigned], key=lambda x: -x["power"]
    )
    for m in remaining_all2:
        eligible = [
            (i, sum(mem["power"] for mem in parties[i]))
            for i in range(num_parties)
            if available_slots(i) > 0
        ]
        if eligible:
            target = min(eligible, key=lambda x: x[1])[0]
        else:
            target = min(range(num_parties), key=lambda i: len(parties[i]))
        assign(m, target)

    # ── 경고: 요정 속성 중복 ────────────────────────────────────────
    for i, party in enumerate(parties):
        seen_attrs: set = set()
        for m in party:
            if m["class"] == "요정" and m["fairy_attr"]:
                if m["fairy_attr"] in seen_attrs:
                    warnings.append(f"파티 {i + 1} 요정 속성 중복 ({m['fairy_attr']})")
                    break
                seen_attrs.add(m["fairy_attr"])

    return parties, warnings


def format_party_embed_fields(parties: List[List[dict]], warnings: List[str]) -> List[dict]:
    """
    Discord embed fields 형태로 변환.
    Returns list of dicts with 'name', 'value', 'inline' keys.
    """
    CLASS_ICON = {"기사": "⚔️", "군주": "🛡️", "마법사": "🔮", "요정": "🧚", "미상": "❓"}
    ATTR_ICON = {"땅": "🌍", "불": "🔥", "바람": "💨", "물": "💧"}

    fields = []
    for i, party in enumerate(parties):
        lines = []
        for j, m in enumerate(party):
            icon = CLASS_ICON.get(m["class"], "❓")
            parts = [f"{icon} **{m['name']}**"]

            if j == 0:
                parts[0] = f"👑 {parts[0]}"

            extras = []
            if m["class"] == "요정" and m["fairy_attr"]:
                attr_icon = ATTR_ICON.get(m["fairy_attr"], "")
                skill_name = ATTR_SKILL_MAP.get(m["fairy_attr"], "")
                skill_str = f" + {skill_name}" if m["fairy_attr_skill"] and skill_name else ""
                extras.append(f"{attr_icon}{m['fairy_attr']}{skill_str}")
            if m["special_skill"]:
                extras.append(m["special_skill"])

            if extras:
                parts.append(f"({', '.join(extras)})")
            lines.append(" ".join(parts))
        fields.append({"name": f"파티 {i + 1}  ({len(party)}명)", "value": "\n".join(lines), "inline": False})

    if warnings:
        fields.append({
            "name": "⚠️ 경고",
            "value": "\n".join(f"⚠️ {w}" for w in warnings),
            "inline": False,
        })

    return fields
