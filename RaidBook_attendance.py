# -*- coding: utf-8 -*-
"""참석 기록 공용 로직 — [참석]/[취소] 로그 라인 생성·리플레이 검증.

보스봇과 전투봇이 같은 로그 포맷과 같은 마감 검증 규칙을 쓰도록 단일화한다.

로그 포맷 (신형, UID 병기):
    [참석] YYYYMMDD 이벤트명 메시지ID | 표시이름 [UID] (총 N명)
구형(UID 없음) 로그도 파싱을 지원한다 — 이름 → user_mapping 역방향 fallback.
검증은 UID 우선이라 클릭~마감 사이 닉네임이 바뀌어도 안전하다.
"""
import os
import re
from datetime import datetime, timedelta

# ════════════════════════════════════════════════════════════════
# 날짜 시트 공식 스키마 — 컬럼을 '위치'가 아닌 '이름'으로 접근하기 위한 레지스트리.
# headers: {필드: [헤더 표기 후보들(신·구)]}, fallback: 헤더가 없을 때의 1-based 위치.
# 물리 컬럼 순서가 파일마다 달라도 이 층을 거치면 코드는 동일해진다.
# ════════════════════════════════════════════════════════════════
BOSS_SCHEMA = {
    "headers": {
        "msg_id":     ["메시지 ID"],
        "event":      ["보스명"],
        "event_time": ["보스 출현 시간"],
        "uid":        ["참석자ID", "참석자 ID"],
        "joined_at":  ["참석 시간"],
        "name":       ["참석자이름", "참석자 이름"],
        "points":     ["획득포인트"],
    },
    "fallback": {"msg_id": 1, "event": 2, "event_time": 3, "uid": 4,
                 "joined_at": 5, "name": 6, "points": 7},
}

COMBAT_SCHEMA = {
    "headers": {
        "msg_id":     ["메시지 ID"],
        "event":      ["전투 내용"],
        "event_time": ["전투 시간"],
        "created_at": ["생성 시각"],
        "name":       ["참석자", "참석자이름", "참석자 이름"],
        "joined_at":  ["참석 시간"],
        "uid":        ["참석자 ID", "참석자ID"],
        "points":     ["획득포인트"],
    },
    "fallback": {"msg_id": 1, "event": 2, "event_time": 3, "created_at": 4,
                 "name": 5, "joined_at": 6, "uid": 7, "points": 8},
}


def resolve_columns(ws, schema):
    """워크시트 1행 헤더를 스키마와 대조해 {필드: 1-based 컬럼} 반환.
    헤더 표기(신·구 모두)를 우선 사용하고, 못 찾은 필드만 fallback 위치."""
    # read_only 워크시트는 max_column이 None일 수 있어 1행을 직접 읽는다
    header_pos = {}
    first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), tuple())
    for c, v in enumerate(first or (), 1):
        if v:
            header_pos[str(v).strip()] = c
    cols = {}
    for field, names in schema["headers"].items():
        col = next((header_pos[n] for n in names if n in header_pos), None)
        cols[field] = col if col else schema["fallback"].get(field)
    return cols


def iter_attend_rows(ws, schema):
    """날짜 시트의 데이터 행을 {필드: 값} dict로 순회 (msg_id 없는 행은 건너뜀)."""
    cols = resolve_columns(ws, schema)
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue
        rec = {}
        for field, col in cols.items():
            rec[field] = row[col - 1] if col and len(row) >= col else None
        if rec.get("msg_id") in (None, ""):
            continue
        yield rec


def attend_log_line(kind, event_label, msg_id, name, uid, total, *, when=None):
    """[참석]/[취소] 로그 라인 생성. kind: "참석" 또는 "취소".
    uid가 숫자가 아니면(임의 이름 수동 추가 등) UID 태그를 생략한다."""
    date_str = (when or datetime.now()).strftime("%Y%m%d")
    uid_tag = f" [{uid}]" if str(uid).isdigit() else ""
    return f"[{kind}] {date_str} {event_label} {msg_id} | {name}{uid_tag} (총 {total}명)"


def _attend_patterns(msg_id):
    """메시지ID 기준 참석/취소 파싱 정규식 쌍 반환.
    이벤트명이 비거나 공백을 포함해도 매칭되도록 .*? 사용.
    그룹1=이름, 그룹2=UID(신형만, 없으면 None)."""
    mid = re.escape(str(msg_id))
    re_join = re.compile(r'\[참석\] \d{8} .*?' + mid + r' \| (.+?)(?: \[(\d+)\])? \(총 \d+명\)')
    re_cancel = re.compile(r'\[취소\] \d{8} .*?' + mid + r' \| (.+?)(?: \[(\d+)\])? \(총 \d+명\)')
    return re_join, re_cancel


def replay_attend_logs(msg_id, log_dir, log_prefix, *, days=2, now=None):
    """최근 N일 로그에서 해당 메시지의 참석/취소 이벤트를 리플레이.
    반환: [uid 또는 이름(구형 로그)] — 최종 참석 상태 순서 유지."""
    re_join, re_cancel = _attend_patterns(msg_id)
    now = now or datetime.now()
    keys = []
    for days_ago in range(days):
        date_str = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        log_path = os.path.join(log_dir, f"{log_prefix}_{date_str}.log")
        if not os.path.exists(log_path):
            continue
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                m = re_join.search(line)
                if m:
                    key = m.group(2) or m.group(1)   # UID 우선, 구형 로그는 이름
                    if key not in keys:
                        keys.append(key)
                    continue
                m = re_cancel.search(line)
                if m:
                    key = m.group(2) or m.group(1)
                    if key in keys:
                        keys.remove(key)
    return keys


def verify_attendees(msg_id, log_dir, log_prefix, json_attendees, name_to_uid,
                     *, logger=None, label=""):
    """로그 리플레이로 참석자 목록 검증. 반환: 최종 참석자 UID 리스트.

    - 로그 매칭 0건이면 JSON 데이터를 신뢰 (구 포맷·로그 회전·재시작 분할 대비)
    - 신형 로그 키는 이미 UID, 구형 이름 키만 name_to_uid 역방향 변환
    - JSON과 다르면 로그 기준으로 교체 (버튼 클릭이 진실의 원천)"""
    json_attendees = list(json_attendees or [])
    if not msg_id:
        return json_attendees

    keys = replay_attend_logs(msg_id, log_dir, log_prefix)

    if not keys:
        # 로그 매칭 0건은 "참석자 0명"의 근거가 될 수 없음 — JSON 신뢰
        if json_attendees and logger:
            logger.info(f"[마감 검증] {label} | 로그 매칭 0건, JSON 데이터 신뢰: {json_attendees}")
        return json_attendees

    uids = [k if k.isdigit() else name_to_uid.get(k, k) for k in keys]

    if set(uids) != set(json_attendees):
        if logger:
            logger.info(f"[마감 검증] {label} | JSON: {json_attendees} → 로그: {uids}")
        return uids

    return json_attendees
