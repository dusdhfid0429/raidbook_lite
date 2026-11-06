"""
RaidBook 참석/득템 JSON 미러 (보스·전투 워크북 공용)

이행기(3~4단계) 전략: 쓰기 지점(~50곳)은 아직 Excel에 기록하므로,
xlsx의 mtime이 바뀔 때마다 전체를 JSON으로 재가져오는 **미러**를 둔다.
- 재가져오기 비용 = 기존 읽기 1회와 동일 (지금은 매 읽기마다 전체 로드)
- 이후 읽기는 캐시(ms 단위) → 상호작용 3초 타임아웃의 주범 제거
- xlsx가 어떤 경로로 수정되든 mtime으로 감지 → 분기(불일치) 위험 없음

Data/store/attend/<워크북이름>.json 구조:
{
  "schema_version": 1, "kind": "attend_mirror", "updated_at": "...",
  "source_mtime": 1753100000.123,
  "sheets": { "2026-07-22": [ {"msg_id": "...", "event": "...", "event_time": "HH:MM",
                                "uid": "...", "name": "...", ...} ] },
  "loot": { "<msg_id>": {"boss_name": "...", "boss_time": "...", "loot_text": "...",
                          "editor": "...", "ts": "...", "ann_id": "...", "acquirers": "..."} }
}
"""
import os
import shutil
import threading
from datetime import datetime

from RaidBook_store_core import JsonStore, store_dir


def _format_time(value):
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    if value is None:
        return ""
    return str(value)


class AttendMirror:
    def __init__(self, excel_path: str, schema: dict, system_sheets: set,
                 loot_sheet: str = "획득 아이템"):
        self.excel_path = excel_path
        self.schema = schema
        self.system_sheets = set(system_sheets)
        self.loot_sheet = loot_sheet
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(excel_path)))
        name = os.path.splitext(os.path.basename(excel_path))[0]
        d = store_dir(base_dir, "attend")
        self._store = JsonStore(
            os.path.join(d, f"{name}.json"),
            default_factory=lambda: {"kind": "attend_mirror", "source_mtime": None,
                                     "sheets": {}, "loot": {}},
            kind="attend_mirror",
        )
        self._import_lock = threading.Lock()

    # ── 가져오기 ──────────────────────────────────────────────────────────
    def _import_all(self) -> dict:
        from openpyxl import load_workbook

        if not os.path.exists(self.excel_path):
            return {"sheets": {}, "loot": {}}
        wb = load_workbook(self.excel_path, read_only=True, data_only=True)
        try:
            return self.import_workbook(wb)
        finally:
            wb.close()

    def import_workbook(self, wb) -> dict:
        """openpyxl 통합문서(파일 또는 메모리) → {"sheets", "loot"}."""
        from RaidBook_attendance import iter_attend_rows

        sheets, loot = {}, {}
        for sn in wb.sheetnames:
            if sn == self.loot_sheet:
                for row in wb[sn].iter_rows(min_row=2, values_only=True):
                    if not row or not row[0]:
                        continue
                    vals = [("" if v is None else v) for v in
                            (list(row) + [""] * 8)[:8]]
                    loot[str(vals[0])] = {
                        "boss_name": str(vals[1]), "boss_time": _format_time(vals[2]),
                        "loot_text": str(vals[3]), "editor": str(vals[4]),
                        "ts": str(vals[5]), "ann_id": str(vals[6]),
                        "acquirers": str(vals[7]),
                    }
                continue
            if sn in self.system_sheets:
                continue
            recs = []
            for rec in iter_attend_rows(wb[sn], self.schema):
                out = {}
                for k, v in rec.items():
                    if v is None:
                        out[k] = ""
                    elif isinstance(v, (str, int, float, bool)):
                        out[k] = v
                    else:
                        out[k] = str(v)  # datetime 등 → 문자열 (JSON 직렬화)
                out["msg_id"] = str(out.get("msg_id", ""))
                out["uid"] = str(out.get("uid", "")) if out.get("uid") else ""
                out["event_time"] = _format_time(rec.get("event_time"))
                recs.append(out)
            if recs:
                sheets[sn] = recs
        return {"sheets": sheets, "loot": loot}

    def _source_mtime(self):
        try:
            return os.path.getmtime(self.excel_path)
        except OSError:
            return None

    @property
    def primary(self) -> bool:
        return os.path.abspath(self.excel_path) in _PRIMARY

    def is_fresh(self) -> bool:
        """재가져오기 없이 바로 읽을 수 있는지 (xlsx mtime이 미러와 같음)."""
        data = self._store.read()
        if self.primary:
            return bool(data.get("primary_since"))
        mtime = self._source_mtime()
        return data.get("source_mtime") == mtime and bool(data.get("sheets") or mtime is None)

    def ensure_fresh(self) -> dict:
        """xlsx가 바뀌었으면 재가져오기 후 데이터 반환. (JSON 원본 모드면 JSON 그대로)"""
        if self.primary:
            return self._ensure_primary()
        data = self._store.read()
        mtime = self._source_mtime()
        if data.get("source_mtime") == mtime and (data.get("sheets") or mtime is None):
            return data
        with self._import_lock:
            data = self._store.read()
            mtime = self._source_mtime()
            if data.get("source_mtime") == mtime and (data.get("sheets") or mtime is None):
                return data
            imported = self._import_all()

            def _apply(d):
                d["sheets"] = imported["sheets"]
                d["loot"] = imported["loot"]
                d["source_mtime"] = mtime
            return self._store.mutate(_apply)

    # ── JSON 원본 모드 ────────────────────────────────────────────────────
    # 평소 기록은 JSON에만 하고, 엑셀은 요청(/save·일일 정리) 때 이 데이터로 만들어 낸다.
    def _ensure_primary(self) -> dict:
        data = self._store.read()
        if data.get("primary_since"):
            return data
        with self._import_lock:
            data = self._store.read()
            if data.get("primary_since"):
                return data
            # 처음 전환할 때 한 번만 기존 엑셀을 그대로 옮겨 온다 (그 순간의 엑셀이 최신 원본)
            imported = self._import_all() if os.path.exists(self.excel_path) else None

            def _apply(d):
                if imported is not None:
                    d["sheets"] = imported["sheets"]
                    d["loot"] = imported["loot"]
                d.setdefault("sheets", {})
                d.setdefault("loot", {})
                d["primary_since"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                d["kind"] = "attend_store"
            return self._write(_apply)

    def _write(self, fn):
        """원본 JSON 변경 — 바꾸기 전 파일을 롤링 백업. fn은 컨테이너를 통째로 바꿔 끼워야 한다
        (다른 스레드가 읽는 중인 리스트/딕셔너리를 제자리에서 고치지 않게)."""
        self._backup()
        return self._store.mutate(fn)

    def _backup(self):
        bdir = _BACKUP_DIRS.get(os.path.abspath(self.excel_path))
        src = self._store.path
        if not bdir or not os.path.exists(src):
            return
        try:
            os.makedirs(bdir, exist_ok=True)
            name = os.path.splitext(os.path.basename(src))[0]
            now = datetime.now()
            # 1분에 1개까지만 (버튼이 몰려도 디스크 부담 없게)
            dst = os.path.join(bdir, f"{name}_{now:%Y%m%d_%H%M}.json")
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            olds = sorted(f for f in os.listdir(bdir)
                          if f.startswith(name + "_") and f.endswith(".json"))
            for f in olds[:-BACKUP_KEEP]:
                try:
                    os.remove(os.path.join(bdir, f))
                except OSError:
                    pass
        except Exception:
            pass

    def snapshot(self, dst_dir, *, keep_days=30):
        """일자별 스냅샷 (하루 1개, keep_days개 유지)."""
        src = self._store.path
        if not os.path.exists(src):
            return
        os.makedirs(dst_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(src))[0]
        shutil.copy2(src, os.path.join(dst_dir, f"{name}_{datetime.now():%Y%m%d}.json"))
        olds = sorted(f for f in os.listdir(dst_dir) if f.startswith(name + "_") and f.endswith(".json"))
        for f in olds[:-keep_days]:
            try:
                os.remove(os.path.join(dst_dir, f))
            except OSError:
                pass

    def upsert_attendance(self, sheet, msg_id, event, event_time, attendees, *,
                          id_to_name, points_fn, now_str, prune=False):
        """참석 기록 upsert — 엑셀판 upsert_attendance_records와 같은 규칙.
        prune=True: 이 알림의 기존 행 중 attendees에 없는 사람을 지움 (attendees가 비면 지우지 않음)."""
        self.ensure_fresh()
        target = str(msg_id)
        attendees = [str(u) for u in (attendees or [])]

        def _apply(d):
            sheets = dict(d.get("sheets") or {})
            recs = list(sheets.get(sheet) or [])
            if prune and attendees:
                keep = set(attendees)
                recs = [r for r in recs if r.get("msg_id") != target or (r.get("uid") and r.get("uid") in keep)]
            mine = [r for r in recs if r.get("msg_id") == target]
            have = {r.get("uid") or "" for r in mine}
            if not attendees:
                if not mine:
                    recs.append(_rec(target, event, event_time, "", now_str))
            else:
                for uid in attendees:
                    if uid in have:
                        continue
                    recs.append(_rec(target, event, event_time, uid, now_str,
                                     name=id_to_name.get(uid, uid), points=points_fn(event, event_time)))
                    have.add(uid)
            sheets[sheet] = recs
            d["sheets"] = sheets
        self._write(_apply)

    def edit_attendees(self, action, sheet, msg_id, event, event_time, targets, *, now_str, points_fn=None):
        """수동 추가/제거. targets: [(uid, 표시이름)]. 반환: 시트가 없으면 False."""
        data = self.ensure_fresh()
        if sheet not in (data.get("sheets") or {}):
            return False
        target = str(msg_id)

        def _apply(d):
            sheets = dict(d.get("sheets") or {})
            recs = list(sheets.get(sheet) or [])
            for uid, display in targets:
                uid = str(uid)
                if action == "add":
                    if any(r.get("msg_id") == target and r.get("uid") == uid for r in recs):
                        continue
                    recs.append(_rec(target, event, event_time, uid, now_str, name=display,
                                     points=points_fn(event, event_time) if points_fn else ""))
                else:
                    for i in range(len(recs) - 1, -1, -1):
                        if recs[i].get("msg_id") == target and recs[i].get("uid") == uid:
                            del recs[i]
                            break
            sheets[sheet] = recs
            d["sheets"] = sheets
        self._write(_apply)
        return True

    def upsert_loot(self, msg_id, boss_name, boss_time, loot_text, editor, *,
                    ann_id=None, now_str="", acquirers=None):
        """득템 기록 upsert. ann_id/acquirers가 None이면 기존 값 유지. 반환: 현재 득템 공지 메시지 ID."""
        self.ensure_fresh()
        target = str(msg_id)
        out = {}

        def _apply(d):
            loot = dict(d.get("loot") or {})
            old = loot.get(target) or {}
            entry = {
                "boss_name": str(boss_name or ""), "boss_time": _format_time(boss_time),
                "loot_text": str(loot_text or ""), "editor": str(editor or ""), "ts": now_str,
                "ann_id": str(ann_id) if ann_id is not None else str(old.get("ann_id", "") or ""),
                "acquirers": str(acquirers) if acquirers is not None else str(old.get("acquirers", "") or ""),
            }
            out["ann"] = entry["ann_id"]
            loot[target] = entry
            d["loot"] = loot
        self._write(_apply)
        return out.get("ann", "")

    def replace_all(self, imported):
        """통합문서에서 다시 읽은 전체 데이터로 교체 (관리 명령이 엑셀 형태로 고친 뒤)."""
        self.ensure_fresh()

        def _apply(d):
            d["sheets"] = imported["sheets"]
            d["loot"] = imported["loot"]
        self._write(_apply)

    def build_workbook(self, raw_headers, loot_headers, *, apply_header=None):
        """JSON → openpyxl 통합문서 (날짜 시트 + 획득 아이템). 통계 시트는 호출부가 채운다."""
        import openpyxl

        data = self.ensure_fresh()
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        order = ["msg_id", "event", "event_time", "uid", "joined_at", "name", "points"]
        for sn in sorted((data.get("sheets") or {}).keys(), reverse=True):
            ws = wb.create_sheet(sn)
            (apply_header or _plain_header)(ws, raw_headers)
            for r in data["sheets"][sn]:
                ws.append([_cell(r.get(k)) for k in order])
                ws.cell(row=ws.max_row, column=1).number_format = "@"
        loot = data.get("loot") or {}
        ws = wb.create_sheet(self.loot_sheet)
        headers = list(loot_headers)
        if len(headers) < 8:
            headers.append("획득자")
        (apply_header or _plain_header)(ws, headers)
        for mid, e in loot.items():
            ws.append([mid, e.get("boss_name", ""), e.get("boss_time", ""), e.get("loot_text", ""),
                       e.get("editor", ""), e.get("ts", ""), e.get("ann_id", ""), e.get("acquirers", "")])
            ws.cell(row=ws.max_row, column=1).number_format = "@"
            ws.cell(row=ws.max_row, column=7).number_format = "@"
        return wb

    # ── 조회 ─────────────────────────────────────────────────────────────
    def loot_entry(self, message_id) -> dict | None:
        return self.ensure_fresh().get("loot", {}).get(str(message_id))

    def iter_sheets(self, only_sheet: str = None, system_sheets: set = None):
        data = self.ensure_fresh()
        skip = set(system_sheets or self.system_sheets)
        sheets = data.get("sheets", {})
        if only_sheet is not None and only_sheet in sheets:
            yield only_sheet, sheets[only_sheet]
            return
        for sn, recs in sheets.items():
            if sn in skip:
                continue
            yield sn, recs


_mirrors = {}
_mirrors_lock = threading.Lock()
_PRIMARY = set()          # JSON이 원본인 워크북 경로들
_BACKUP_DIRS = {}         # 경로 → JSON 롤링 백업 폴더
BACKUP_KEEP = 30


def set_primary(excel_path: str, backup_dir: str = None):
    """이 워크북은 JSON이 원본 — 평소 쓰기는 JSON에만, 엑셀은 내보내기로만 만든다."""
    key = os.path.abspath(excel_path)
    _PRIMARY.add(key)
    if backup_dir:
        _BACKUP_DIRS[key] = backup_dir


def _rec(msg_id, event, event_time, uid, joined_at, *, name="", points=""):
    return {"msg_id": str(msg_id), "event": str(event or ""), "event_time": _format_time(event_time),
            "uid": str(uid or ""), "joined_at": joined_at or "", "name": name or "", "points": points}


def _cell(v):
    return None if v == "" else v


def _plain_header(ws, headers):
    ws.append(list(headers))


def get_mirror(excel_path: str, schema: dict, system_sheets: set,
               loot_sheet: str = "획득 아이템") -> AttendMirror:
    key = os.path.abspath(excel_path)
    with _mirrors_lock:
        if key not in _mirrors:
            _mirrors[key] = AttendMirror(excel_path, schema, system_sheets, loot_sheet)
        return _mirrors[key]
