# -*- coding: utf-8 -*-
"""멤버 정보 공용 라이브러리 — member_profiles.json 단일 창구.

서버 멤버의 스펙 프로필(클래스/레벨/무기·방어구 강화/요정 속성/특수스킬)을
여러 기능이 제각각 읽고 쓰던 것을 한 모듈로 일원화한다.

사용처:
- 파티 구성 (RaidBook_party — 전투력 계산)
- 설문 수집 (RaidBook_member_profile — Watchdog !survey)
- 공성 참석 체크 v3 (RaidBook_siege — 참석 입력값 자동 반영)
- Watchdog !profiles Excel 내보내기

프로필 스키마 (모두 선택 필드):
  class(str) · level(int) · weapon_enh(int) · armor_enh(int)
  fairy_attr(str) · fairy_attr_skill(bool) · special_skill(str)
  gear_note(str, 자유 서술 원문) · updated_at · updated_from
"""
import json
import os
import re
from datetime import datetime

from RaidBook_common import write_json_atomic

CLASS_ALIASES = {"기사": "기사", "요정": "요정", "법사": "마법사", "마법사": "마법사",
                 "군주": "군주", "다크엘프": "다크엘프", "닼엘": "다크엘프"}


# ── 저장/로드 ────────────────────────────────────────────────────────────────

def profile_path(base_dir: str) -> str:
    return os.path.join(base_dir, "Data", "member_profiles.json")


def load_profiles(base_dir: str) -> dict:
    path = profile_path(base_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_profile(base_dir: str, uid) -> dict:
    return dict(load_profiles(base_dir).get(str(uid)) or {})


def save_profile(base_dir: str, uid, profile: dict, *, source: str = ""):
    """프로필 통째 교체 (설문 완료 등). updated_at/updated_from 자동 기록."""
    data = load_profiles(base_dir)
    p = dict(profile)
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if source:
        p["updated_from"] = source
    data[str(uid)] = p
    write_json_atomic(profile_path(base_dir), data)


def update_profile(base_dir: str, uid, updates: dict, *, source: str = ""):
    """부분 갱신 — 넘긴 필드만 덮어쓰고 기존 필드(fairy_attr 등)는 보존."""
    data = load_profiles(base_dir)
    p = dict(data.get(str(uid)) or {})
    p.update({k: v for k, v in updates.items() if v is not None})
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if source:
        p["updated_from"] = source
    data[str(uid)] = p
    write_json_atomic(profile_path(base_dir), data)


# ── 정규화/파싱 유틸 ─────────────────────────────────────────────────────────

def normalize_class(raw) -> str:
    cls = str(raw or "").strip()
    return CLASS_ALIASES.get(cls, cls)


def parse_level(raw):
    """'42' / '랩38' → int. 실패 시 None."""
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    return int(digits) if digits else None


def parse_gear_enh(raw):
    """장비 자유 서술에서 (무기 강화, 방어구 강화) 추출. 예: '무기 +9 / 방어구 +6', '9/6'.
    실패한 쪽은 None."""
    gear = str(raw or "")
    m_w = re.search(r"무기[^0-9+\-]*\+?\s*(\d+)", gear)
    m_a = re.search(r"방어[구]?[^0-9+\-]*\+?\s*(\d+)", gear)
    if m_w or m_a:
        return (int(m_w.group(1)) if m_w else None,
                int(m_a.group(1)) if m_a else None)
    m2 = re.search(r"(\d+)\s*/\s*(\d+)", gear)   # "9/6" 축약: 앞=무기, 뒤=방어구
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return None, None


def normalize_skill(raw) -> str:
    skill = str(raw or "").strip()
    return "" if skill in ("-", "없음", "x", "X") else skill


def update_profile_from_freeform(base_dir: str, uid, form: dict, *, source: str):
    """자유 입력(랩/클래스/장비/특수스킬 문자열) → 정규화 후 프로필 부분 갱신.
    공성 참석 체크·특수입찰 등 모달 입력 공용."""
    updates = {}
    lvl = parse_level(form.get("level"))
    if lvl is not None:
        updates["level"] = lvl
    cls = normalize_class(form.get("class"))
    if cls:
        updates["class"] = cls
    w, a = parse_gear_enh(form.get("gear"))
    if w is not None:
        updates["weapon_enh"] = w
    if a is not None:
        updates["armor_enh"] = a
    gear_raw = str(form.get("gear") or "").strip()
    if gear_raw:
        updates["gear_note"] = gear_raw     # 원문 보존 (파싱 실패 대비)
    updates["special_skill"] = normalize_skill(form.get("skill"))
    update_profile(base_dir, uid, updates, source=source)


# ── 파생 값 ──────────────────────────────────────────────────────────────────

def calc_power(profile: dict) -> int:
    """파티 구성용 전투력 산식 — 레벨 + 무기 강화 + 방어구 강화 (단일 정의)."""
    p = profile or {}
    return int(p.get("level", 0) or 0) + int(p.get("weapon_enh", 0) or 0) \
        + int(p.get("armor_enh", 0) or 0)
