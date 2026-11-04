# -*- coding: utf-8 -*-
"""혈맹 구분 — 한 디스코드 서버를 여러 혈맹이 같이 쓸 때 참석자를 혈맹 역할별로 묶어 표시.

디스코드 의존 없는 순수 로직 (보스봇이 멤버 → 역할 이름 조회 함수를 넘겨 준다).
"""

DEFAULT_CLAN_ROLES = [
    {"role": "정도", "emoji": "⚔️"},
    {"role": "슈퍼맨", "emoji": "🦸"},
]
UNASSIGNED_LABEL = "❔ 미지정"
BOTH_ROLES_MARK = " ⚠️"


def parse_clan_roles(raw) -> list:
    """config clan_roles → [(역할이름, 이모지)]. 키가 없으면(None) 기본값, 빈 목록이면 혈맹 구분 끔.
    항목은 {"role": "정도", "emoji": "⚔️"} 또는 "정도" 문자열."""
    if raw is None:
        raw = DEFAULT_CLAN_ROLES
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            name = str(item.get("role") or item.get("name") or "").strip()
            emoji = str(item.get("emoji") or "").strip()
        else:
            name, emoji = str(item or "").strip(), ""
        if name and name not in [n for n, _ in out]:
            out.append((name, emoji))
    return out


def split_names(names_text) -> list:
    """make_attend_embed에 넘어오는 '이름1, 이름2' 문자열 → 이름 목록 ('없음'·빈 값은 빈 목록)."""
    text = str(names_text or "").strip()
    if not text or text == "없음":
        return []
    return [n.strip() for n in text.split(", ") if n.strip()]


def group_by_clan(names, clan_roles, roles_of) -> tuple:
    """참석자 이름 목록을 혈맹별로 나눈다.
    roles_of(이름) → 그 멤버의 역할 이름 집합 (못 찾으면 빈 집합).
    반환: ([(혈맹명, 이모지, [이름…])…], [미지정 이름…]). 혈맹 순서는 clan_roles 순서.
    역할이 둘 이상이면 앞쪽 혈맹에만 넣고 이름 뒤에 ⚠️ (중복 집계 방지)."""
    groups = [(name, emoji, []) for name, emoji in clan_roles]
    unassigned = []
    for person in names:
        try:
            roles = set(roles_of(person) or ())
        except Exception:
            roles = set()
        hits = [i for i, (name, _) in enumerate(clan_roles) if name in roles]
        if not hits:
            unassigned.append(person)
        else:
            groups[hits[0]][2].append(person + (BOTH_ROLES_MARK if len(hits) > 1 else ""))
    return groups, unassigned


def format_clan_block(prefix, total, groups, unassigned) -> str:
    """'✅ 현재 참석: 총 N명' + 혈맹별 줄 + (있으면) 미지정 줄."""
    lines = [f"{prefix}: 총 {total}명"]
    for name, emoji, members in groups:
        label = f"{emoji} {name}" if emoji else name
        lines.append(f"{label} ({len(members)}명) | {', '.join(members) if members else '없음'}")
    if unassigned:
        lines.append(f"{UNASSIGNED_LABEL} ({len(unassigned)}명) | {', '.join(unassigned)}")
    return "\n".join(lines)


def format_clan_counts(groups, unassigned) -> str:
    """관리자 확인용 한 줄 요약 '정도 4 · 슈퍼맨 2 · 미지정 1'."""
    parts = [f"{name} {len(members)}" for name, _, members in groups]
    if unassigned:
        parts.append(f"미지정 {len(unassigned)}")
    return " · ".join(parts)


FIELD_LIMIT = 1024   # 디스코드 embed 필드 값 최대 길이


# 닉네임 뒤에 붙는 직업 표기 — 짧은 이름을 고를 때 건너뛴다
CLASS_WORDS = {"기사", "요정", "법사", "군주", "다엘", "다크엘프", "용기사", "환술사", "전사", "검사",
               "창기사", "광전사", "창술사", "벌"}
NAME_SEP = " · "


def short_name(person, clan_names=()) -> str:
    """폰에서 짧게 보이도록 닉네임만: '[정도]투신(애마)/기사/90' → '투신(애마)', '[슈퍼맨]빼쥬 법사 88' → '빼쥬'.
    혈맹 표기·직업·숫자 조각은 건너뛰고 첫 이름 조각을 쓴다 (전부 직업이면 첫 조각, 아무것도 없으면 원래 이름)."""
    text = str(person).strip()
    body = text.lstrip("@").strip()
    for clan in clan_names:
        for tag in (f"[{clan}]", f"{clan}/"):
            if body.startswith(tag):
                body = body[len(tag):].strip()
    warn = body.endswith(BOTH_ROLES_MARK.strip())
    if warn:
        body = body[:-len(BOTH_ROLES_MARK.strip())].strip()
    tokens = [t for t in body.replace("/", " ").split() if not t.isdigit() and t not in clan_names]
    # 직업 단어가 아닌 첫 조각, 전부 직업 단어면('군주/벌/82') 첫 조각
    pick = next((t for t in tokens if t not in CLASS_WORDS), tokens[0] if tokens else "")
    return pick + (BOTH_ROLES_MARK if warn else "") if pick else text


def _chunk_joined(names, limit=FIELD_LIMIT) -> list:
    """이름들을 ' · '로 이어 붙이되 필드 길이 제한에 맞춰 여러 덩어리로."""
    chunks, cur = [], ""
    for name in names:
        name = name[:limit]
        if cur and len(cur) + len(NAME_SEP) + len(name) > limit:
            chunks.append(cur)
            cur = name
        else:
            cur = f"{cur}{NAME_SEP}{name}" if cur else name
    if cur:
        chunks.append(cur)
    return chunks or ["없음"]


def clan_fields(groups, unassigned) -> list:
    """혈맹별 embed 필드 [(필드 이름, 값)] — 닉네임만 ' · '로 이어 붙여 폰에서도 몇 줄로.
    길어서 나뉘면 이어지는 필드 이름은 빈칸(\u200b). 미지정은 있을 때만."""
    clan_names = tuple(name for name, _, _ in groups)
    cols = [((f"{emoji} {name}" if emoji else name), members) for name, emoji, members in groups]
    if unassigned:
        cols.append((UNASSIGNED_LABEL, list(unassigned)))
    out = []
    for label, people in cols:
        shorts = [short_name(p, clan_names) for p in people]
        for i, chunk in enumerate(_chunk_joined(shorts)):
            out.append((f"{label} {len(people)}명" if i == 0 else "\u200b", chunk))
    return out
