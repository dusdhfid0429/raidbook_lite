# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import shutil
import time
import zipfile
import json
from datetime import datetime


META_SHEET_NAME = "_meta"


class DailyFileHandler(logging.Handler):
    """일자별 로그 파일 핸들러."""

    def __init__(self, log_dir, prefix):
        super().__init__()
        self.log_dir = log_dir
        self.prefix = prefix
        self._current_date = None
        self._file_handler = None

    def _get_handler(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._file_handler:
                self._file_handler.close()
            log_path = os.path.join(self.log_dir, f"{self.prefix}_{today}.log")
            self._file_handler = logging.FileHandler(log_path, encoding="utf-8")
            self._file_handler.setFormatter(self.formatter)
            self._current_date = today
        return self._file_handler

    def emit(self, record):
        handler = self._get_handler()
        handler.emit(record)

    def close(self):
        if self._file_handler:
            self._file_handler.close()
        super().close()


def is_valid_xlsx(path):
    """xlsx 파일이 정상 zip 구조이고 worksheet 가 있는지 검증."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            return "[Content_Types].xml" in names and any(n.startswith("xl/worksheets/") for n in names)
    except Exception:
        return False


def ensure_workbook_meta(wb, schema_name, schema_version):
    """워크북에 숨김 _meta 시트를 만들고 스키마 버전 정보를 기록한다."""
    ws = wb[META_SHEET_NAME] if META_SHEET_NAME in wb.sheetnames else wb.create_sheet(META_SHEET_NAME)
    ws.sheet_state = "hidden"
    values = {
        "schema_name": schema_name,
        "schema_version": int(schema_version),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    ws.cell(row=1, column=1, value="key")
    ws.cell(row=1, column=2, value="value")
    for row_idx, (key, value) in enumerate(values.items(), start=2):
        ws.cell(row=row_idx, column=1, value=key)
        ws.cell(row=row_idx, column=2, value=value)
    return ws


def get_workbook_meta(wb):
    """숨김 _meta 시트의 key/value를 dict로 반환한다."""
    if META_SHEET_NAME not in wb.sheetnames:
        return {}
    ws = wb[META_SHEET_NAME]
    data = {}
    for key, value in ws.iter_rows(min_row=2, max_col=2, values_only=True):
        if key:
            data[str(key)] = value
    return data


async def save_workbook_safely(wb, path, label="엑셀", logger=None):
    """임시 xlsx로 저장/검증한 뒤 원본을 교체한다."""
    temp_path = f"{path}.tmp"
    last_err = None
    for delay in (0, 1, 2, 4):
        if delay:
            await asyncio.sleep(delay)
        try:
            wb.save(temp_path)
            if not is_valid_xlsx(temp_path):
                raise ValueError(f"{label} 임시 저장 파일 검증 실패")
            os.replace(temp_path, path)
            return True, None
        except Exception as e:
            last_err = e
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as cleanup_err:
                if logger:
                    logger.info(f"[{label} 임시 파일 삭제 실패] {cleanup_err}")
    return False, last_err


def save_workbook_safely_sync(wb, path, label="엑셀", logger=None, *, raise_on_error=False):
    """동기 코드에서 사용하는 안전 저장."""
    temp_path = f"{path}.tmp"
    last_err = None
    for delay in (0, 1, 2, 4):
        if delay:
            time.sleep(delay)
        try:
            wb.save(temp_path)
            if not is_valid_xlsx(temp_path):
                raise ValueError(f"{label} 임시 저장 파일 검증 실패")
            os.replace(temp_path, path)
            if raise_on_error:
                return None
            return True, None
        except Exception as e:
            last_err = e
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as cleanup_err:
                if logger:
                    logger.info(f"[{label} 임시 파일 삭제 실패] {cleanup_err}")
    if raise_on_error:
        raise last_err
    return False, last_err


def do_rolling_backup(src_path, backup_dir, prefix, logger=None, *, extra_skip_prefix=None, keep=10):
    """src_path를 backup_dir에 타임스탬프 파일로 복사하고 최근 keep개만 유지."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"{prefix}_{ts}.xlsx")
    shutil.copy2(src_path, backup_path)
    if logger:
        logger.info(f"[백업] {backup_path}")

    rolling = sorted([
        f for f in os.listdir(backup_dir)
        if f.startswith(prefix + "_") and f.endswith(".xlsx")
        and (extra_skip_prefix is None or not f.startswith(extra_skip_prefix))
        and os.path.isfile(os.path.join(backup_dir, f))
    ], reverse=True)
    for old in rolling[keep:]:
        try:
            os.remove(os.path.join(backup_dir, old))
            if logger:
                logger.info(f"[백업 삭제-롤링] {old}")
        except Exception as e:
            if logger:
                logger.info(f"[백업 삭제 실패] {old}: {e}")


def do_daily_snapshot(src_path, daily_dir, prefix, logger=None, *, keep_days=30):
    """src_path를 daily_dir에 1일 1파일(YYYYMMDD)로 저장하고 최근 keep_days만 유지."""
    date_str = datetime.now().strftime("%Y%m%d")
    snapshot_path = os.path.join(daily_dir, f"{prefix}_{date_str}.xlsx")
    shutil.copy2(src_path, snapshot_path)
    if logger:
        logger.info(f"[일자 백업] {snapshot_path}")

    daily = sorted([
        f for f in os.listdir(daily_dir)
        if f.startswith(prefix + "_") and f.endswith(".xlsx")
    ], reverse=True)
    for old in daily[keep_days:]:
        try:
            os.remove(os.path.join(daily_dir, old))
            if logger:
                logger.info(f"[일자 백업 삭제] {old}")
        except Exception as e:
            if logger:
                logger.info(f"[일자 백업 삭제 실패] {old}: {e}")


def restore_latest_backup(dst_path, search_dirs, prefix, logger=None, *, skip_prefixes=()):
    """search_dirs에서 최신 정상 xlsx 백업을 찾아 dst_path로 복구한다."""
    candidates = []
    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not (name.startswith(prefix + "_") and name.endswith(".xlsx")):
                continue
            if any(name.startswith(skip) for skip in skip_prefixes):
                continue
            full_path = os.path.join(directory, name)
            if os.path.isfile(full_path):
                candidates.append((os.path.getmtime(full_path), full_path))

    candidates.sort(reverse=True)
    for _ts, path in candidates:
        if is_valid_xlsx(path):
            shutil.copy2(path, dst_path)
            if logger:
                logger.info(f"[복구] {path}")
            return True

    if logger:
        logger.info("[복구 실패] 정상 백업이 존재하지 않음")
    return False


def write_json_atomic(path, data, logger=None, *, ensure_ascii=False, indent=2):
    """JSON 파일을 임시 파일에 쓴 뒤 원자적으로 교체한다.

    Windows 에서 os.replace() 가 WinError 32 (파일 사용 중) 로 실패할 수 있으므로
    최대 3 회 재시도한다. 임시 파일 정리 실패는 무시(다음 쓰기 시 덮어씌워짐).
    """
    import time as _time
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # Windows 에서 파일 핸들이 완전히 해제되기 전에 replace 가 실패하는 경우 재시도
        last_err = None
        for _attempt in range(3):
            try:
                os.replace(temp_path, path)
                last_err = None
                break
            except OSError as e:
                last_err = e
                _time.sleep(0.05 * (_attempt + 1))   # 50 ms, 100 ms, 150 ms
        if last_err:
            raise last_err
    except Exception:
        # 임시 파일 정리 — 실패해도 다음 쓰기 시 덮어씌워지므로 로그 없이 무시
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise
