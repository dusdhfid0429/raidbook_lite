"""
RaidBook JSON 저장 계층 — 공통 코어

Excel(source of truth) → JSON(source of truth) 전환의 기반 모듈.
- load_json / save_json: 중앙 JSON 입출력 (원자적 저장 + 백업 연동)
- JsonStore: mtime 기반 캐시 + 프로세스 내 락으로 읽기/변경을 감싼다

설계 원칙:
- 쓰기는 RaidBook_common.write_json_atomic (tmp → fsync → os.replace) 재사용
- 도메인 파일은 소유 봇만 쓴다 (기존 xlsx 운영 원칙과 동일)
- 파일에는 schema_version / kind / updated_at 메타를 자동 주입
"""
import os
import json
import threading
from datetime import datetime

from RaidBook_common import write_json_atomic, do_rolling_backup

SCHEMA_VERSION = 1


def store_dir(base_dir: str, *parts: str) -> str:
    """Data/store/ 아래 경로. 없으면 생성."""
    path = os.path.join(base_dir, "Data", "store", *parts)
    os.makedirs(os.path.dirname(path) if parts and "." in parts[-1] else path,
                exist_ok=True)
    return path


def load_json(path: str, default=None):
    """중앙 JSON 로더. 없거나 손상 시 default 반환 (예외 없음)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        # 손상 파일 — .bak 이 있으면 복구 시도
        bak = path + ".bak"
        try:
            with open(bak, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def save_json(path: str, data, *, kind: str = None, backup_subfolder: str = None,
              backup_keep: int = 10):
    """중앙 JSON 저장. 메타 주입 + (옵션) 롤링 백업 후 원자적 저장."""
    if isinstance(data, dict):
        data.setdefault("schema_version", SCHEMA_VERSION)
        if kind:
            data["kind"] = kind
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if backup_subfolder and os.path.exists(path):
        try:
            do_rolling_backup(path, subfolder=backup_subfolder, keep=backup_keep)
        except Exception:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, data)


class JsonStore:
    """파일 1개를 감싸는 저장소. mtime 캐시 + 프로세스 내 직렬화.

    사용:
        store = JsonStore(path, default_factory=lambda: {"records": []}, kind="siege")
        data = store.read()                 # 캐시된 dict (수정 금지)
        store.mutate(lambda d: d["records"].append(rec))
    """

    def __init__(self, path: str, *, default_factory, kind: str = None,
                 backup_subfolder: str = None):
        self.path = path
        self.kind = kind
        self.backup_subfolder = backup_subfolder
        self._default_factory = default_factory
        self._lock = threading.Lock()
        self._cache = None
        self._cache_mtime = None

    def _load_unlocked(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        data = load_json(self.path, default=None)
        if data is None:
            data = self._default_factory()
        self._cache = data
        self._cache_mtime = mtime
        return data

    def read(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def mutate(self, fn):
        """읽기 → fn(data) → 원자적 저장. fn이 False를 반환하면 저장 생략."""
        with self._lock:
            data = self._load_unlocked()
            result = fn(data)
            if result is False:
                return data
            save_json(self.path, data, kind=self.kind,
                      backup_subfolder=self.backup_subfolder)
            try:
                self._cache_mtime = os.path.getmtime(self.path)
            except OSError:
                self._cache_mtime = None
            self._cache = data
            return data
