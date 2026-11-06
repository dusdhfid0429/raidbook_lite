# -*- coding: utf-8 -*-
"""보스 데이터 JSON 원본 모드 — 기존 엑셀 자동 이전, 평소 저장은 JSON만, 요청 시 엑셀 내보내기."""
import asyncio
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import openpyxl  # noqa: E402
import RaidBook_boss_Bot as b  # noqa: E402

TMP = tempfile.mkdtemp(prefix="rb_store_")


def _use_dir(name):
    data = os.path.join(TMP, name, "Data")
    os.makedirs(os.path.join(data, "Backup", "Boss", "Daily"), exist_ok=True)
    b.EXCEL_PATH = os.path.join(data, "RaidBook_Boss_Data.xlsx")
    b.BOSS_BACKUP_DIR = os.path.join(data, "Backup", "Boss")
    b.BOSS_BACKUP_DAILY_DIR = os.path.join(data, "Backup", "Boss", "Daily")
    return data


def _make_old_excel(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026-11-01"
    ws.append(b.RAW_HEADERS)
    ws.append(["111", "보스A", "12:00", "1001", "2026-11-01 12:00:00", "철수", 1])
    ws.append(["111", "보스A", "12:00", "1002", "2026-11-01 12:00:00", "영희", 1])
    lw = wb.create_sheet(b.LOOT_SHEET)
    lw.append(b.LOOT_HEADERS + ["획득자"])
    lw.append(["111", "보스A", "12:00", "반지", "철수", "2026-11-01 12:30:00", "999", "영희"])
    wb.save(path)


def _recs(mid):
    return [r for _sn, r in b._iter_boss_records() if r["msg_id"] == str(mid)]


def test_migrates_existing_excel_once():
    _use_dir("migrate")
    _make_old_excel(b.EXCEL_PATH)
    assert sorted(r["uid"] for r in _recs(111)) == ["1001", "1002"]
    assert b.load_loot_from_excel(111)[0] == "반지"
    assert b.load_loot_acquirers_from_excel(111) == "영희"
    # 이전 후 엑셀을 고쳐도 JSON이 원본이라 다시 읽지 않는다
    wb = openpyxl.load_workbook(b.EXCEL_PATH)
    wb["2026-11-01"].append(["222", "보스B", "13:00", "1003", "", "민수", 1])
    wb.save(b.EXCEL_PATH)
    assert not _recs(222)
    print("✓ test_migrates_existing_excel_once")


def test_saves_go_to_json_only_and_export_on_request():
    _use_dir("save")
    _make_old_excel(b.EXCEL_PATH)
    b._boss_store().ensure_fresh()
    mtime = os.path.getmtime(b.EXCEL_PATH)

    async def run():
        await b.save_boss_attendees_to_excel(333, "보스C", "18:00", ["1001", "1004"], "2026-11-02")
        ok, ann = await b.save_boss_loot_to_excel(333, "보스C", "18:00", "목걸이", "운영진", announcement_msg_id=555)
        assert ok and ann == "555"
        ok, ann = await b.save_boss_loot_to_excel(333, "보스C", "18:00", "목걸이 x2", "운영진")
        assert ann == "555"   # 공지 ID는 유지
        # 취소 반영(prune)
        await b.save_boss_attendees_to_excel(333, "보스C", "18:00", ["1004"], "2026-11-02", prune=True)
    asyncio.run(run())
    assert [r["uid"] for r in _recs(333)] == ["1004"]
    assert os.path.getmtime(b.EXCEL_PATH) == mtime, "평소 저장에서 엑셀 파일을 쓰면 안 됨"

    # ➕/➖ 수동 수정
    assert b._apply_attendee_edits_excel_sync("add", "333", "보스C", "18:00", "2026-11-02",
                                              [("1005", "지수")], ["1004"]) is None
    assert sorted(r["uid"] for r in _recs(333)) == ["1004", "1005"]
    assert b._apply_attendee_edits_excel_sync("remove", "333", "보스C", "18:00", "2026-11-02",
                                              [("1004", "")], ["1004", "1005"]) is None
    assert [r["uid"] for r in _recs(333)] == ["1005"]

    # 조회
    assert b.load_boss_name_from_excel(333) == "보스C" and b.load_boss_time_from_excel(333) == "18:00"
    assert b._find_boss_record_by_id("333")[4] == ["1005"]

    # 요청 시 내보내기 → 엑셀 파일 최신화 (통계 시트 포함), 다시 읽어도 같은 데이터
    ok, err = b.export_boss_excel_sync()
    assert ok, err
    wb = openpyxl.load_workbook(b.EXCEL_PATH)
    for sn in ("2026-11-01", "2026-11-02", b.LOOT_SHEET, "히스토리", "누적 통계"):
        assert sn in wb.sheetnames, (sn, wb.sheetnames)
    again = b._boss_store().import_workbook(wb)
    data = b._boss_store().ensure_fresh()
    key = lambda d: sorted((sn, r["msg_id"], r["uid"]) for sn, rs in d["sheets"].items() for r in rs)
    assert key(again) == key(data)
    assert again["loot"]["333"]["loot_text"] == "목걸이 x2" and again["loot"]["333"]["ann_id"] == "555"

    # 관리 명령 경로(통합문서로 고친 뒤 반영)도 데이터가 그대로 유지
    ok, _ = b._commit_wb(b._wb_from_store())
    assert ok and key(b._boss_store().ensure_fresh()) == key(data)
    print("✓ test_saves_go_to_json_only_and_export_on_request")


def test_fresh_install_without_excel():
    _use_dir("fresh")
    assert not os.path.exists(b.EXCEL_PATH)
    asyncio.run(b.save_boss_attendees_to_excel(444, "보스D", "20:00", ["1001"], "2026-11-03"))
    assert [r["uid"] for r in _recs(444)] == ["1001"]
    assert b.load_attendees_from_excel(444) == ["1001"]
    assert b.load_expired_message_bundle(444)["boss_name"] == "보스D"
    ok, err, _, _ = b._tidy_and_export_sync({}, map_user_ids=False)
    assert ok, err
    assert os.path.exists(b.EXCEL_PATH)
    b.backup_excel_daily_snapshot()
    assert any(f.endswith(".json") for f in os.listdir(b.BOSS_BACKUP_DAILY_DIR))
    print("✓ test_fresh_install_without_excel")


if __name__ == "__main__":
    try:
        test_migrates_existing_excel_once()
        test_saves_go_to_json_only_and_export_on_request()
        test_fresh_install_without_excel()
        print("all passed")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
