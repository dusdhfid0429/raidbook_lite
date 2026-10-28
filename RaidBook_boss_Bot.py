# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import ui
from datetime import datetime, timedelta
import asyncio
import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import os
import sys
import signal
import json
import re
import logging
import threading
from collections import defaultdict
from RaidBook_storage import (
    DailyFileHandler,
    apply_border_all as _apply_border_all_common,
    apply_header as _apply_header_common,
    auto_column_width as _auto_column_width_common,
    do_daily_snapshot,
    do_rolling_backup,
    ensure_workbook_meta,
    is_valid_xlsx as _is_valid_xlsx,
    restore_latest_backup,
    save_workbook_safely as _save_workbook_safely,
    save_workbook_safely_sync as _save_workbook_safely_sync,
    write_json_atomic,
)
from RaidBook_ipc import send_ipc, write_json_response
from RaidBook_images import normalized_image_file_args, parse_canvas_size
import RaidBook_schedule as rb_schedule
import RaidBook_attendance as rb_att
from RaidBook_boss_data import (
    ensure_loot_sheet as _ensure_loot_sheet_core,
    load_attendees_by_message_id as _load_attendees_by_message_id_core,
    load_loot_record as _load_loot_record_core,
    load_loot_acquirers as _load_loot_acquirers_core,
    upsert_attendance_records as _upsert_attendance_records_core,
    upsert_loot_record as _upsert_loot_record_core,
)
from RaidBook_boss_auction import (
    AuctionSetupModal,
    AuctionView,
    BoardView,
    BossAuction,
    LotCreateModal,
    auction_options,
    board_results_text,
    lots_results_text,
    merge_board_lots,
    parse_board_defaults,
)
from RaidBook_loot import (
    begin_loot_processing,
    bump_loot_revision,
    delete_loot_announcement as _delete_loot_announcement_common,
    end_loot_processing,
    find_loot_announcement_message as _find_loot_announcement_message_common,
    get_loot_revision,
    is_loot_processing,
    loot_busy_message,
    post_or_update_loot_announcement as _post_or_update_loot_announcement_common,
)
from RaidBook_users import UserStore
from RaidBook_party import compose_parties, format_party_embed_fields, load_party_config
from config_helper import (
    ensure_config_from_template,
    ensure_runtime_tree,
    get_app_dir,
    get_config_path,
    get_runtime_dir,
)

# config 로드 (스크립트 위치 기준으로 Config 폴더 탐색)
_SCRIPT_DIR = get_app_dir()
_RUNTIME_DIR = get_runtime_dir()
ensure_runtime_tree()
CONFIG_PATH = get_config_path("boss_config.json")

# 설정 파일 자동 생성 (템플릿에서)
if not ensure_config_from_template(CONFIG_PATH):
    print(f"❌ 설정 파일을 찾을 수 없습니다: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# 경로 설정 (config의 base_dir 기준, 없으면 스크립트 위치)
BASE_DIR = config.get("base_dir") or _RUNTIME_DIR
# 파티 config (party_config.json) — 없으면 기본값 사용
_PARTY_CONFIG_PATH = get_config_path("party_config.json")
ensure_config_from_template(_PARTY_CONFIG_PATH)
_party_cfg = load_party_config(BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "Data")
BACKUP_DIR = os.path.join(DATA_DIR, "Backup")
BOSS_BACKUP_DIR = os.path.join(BACKUP_DIR, "Boss")          # 당일 롤링 백업 (현재 상태)
BOSS_BACKUP_DAILY_DIR = os.path.join(BOSS_BACKUP_DIR, "Daily")  # 자정 일자별 스냅샷
os.makedirs(BOSS_BACKUP_DIR, exist_ok=True)
os.makedirs(BOSS_BACKUP_DAILY_DIR, exist_ok=True)
LOG_DIR = os.path.join(BASE_DIR, "Log")
USER_MAPPING_PATH = os.path.join(DATA_DIR, "user_mapping.json")

# 폴더 생성
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


_USER_STORE = None


def _user_store():
    global _USER_STORE
    if _USER_STORE is None:
        _USER_STORE = UserStore(DATA_DIR, globals().get("log"))
    return _USER_STORE


def update_user_mapping(uid: str, name: str):
    """uid → display_name 매핑을 user_mapping.json에 갱신 (key=uid, value=name)"""
    try:
        _user_store().update_mapping(uid, name)
    except Exception as e:
        log.info(f"[유저 매핑] 업데이트 실패: {e}")


async def update_mapping_from_guild(guild) -> int:
    """서버 멤버 전체를 읽어 user_mapping.json 갱신 (uid→name 형식). 변경된 항목 수 반환"""
    try:
        mapping = load_user_mapping()
        updated = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            str_id = str(member.id)
            if mapping.get(str_id) != member.display_name:
                mapping[str_id] = member.display_name
                updated += 1
        if updated > 0:
            _user_store().save_mapping(mapping)
        return updated
    except Exception as e:
        log.info(f"[유저 매핑] 서버 멤버 갱신 실패: {e}")
        return 0


def load_user_mapping() -> dict:
    """user_mapping.json 로드. 없으면 빈 dict 반환"""
    return _user_store().load_mapping()


def _load_id_to_name_mapping() -> dict:
    """user_mapping.json 로드 → {uid: name} 반환"""
    return _user_store().id_to_name()


def _name_to_uid_mapping() -> dict:
    """user_mapping.json에서 역방향 {name: uid} 반환"""
    return _user_store().name_to_uid()


_UID_ALIAS_PATH = os.path.join(DATA_DIR, "uid_alias.json")
_LOGGED_ALIAS_HITS = set()   # 프로세스 단위 dedupe (같은 alias 매핑은 1회만 로그)

def _load_uid_alias() -> dict:
    """uid_alias.json 로드 → {redundant_uid: canonical_uid}.
    같은 사람의 여러 디스코드 계정 / 동명이인 통합 처리.
    `_`로 시작하는 키는 메타데이터(주석)."""
    return _user_store().load_alias()

def _alias_notes() -> dict:
    """uid_alias.json의 _notes 블록(alias 종류 설명) 반환."""
    return _user_store().alias_notes()

def _resolve_uid(uid) -> str:
    """uid에 alias가 등록돼 있으면 정규 UID로 치환해 반환.
    실제 치환이 발생한 alias 매핑은 프로세스당 1회 로그."""
    return _user_store().resolve_uid(uid)


def _auto_migrate_user_mapping():
    """user_mapping.json이 {name: uid} 구형식이면 {uid: name}으로 자동 변환"""
    migrated = _user_store().auto_migrate_mapping()
    if migrated:
        log.info(f"[유저 매핑] 자동 마이그레이션 완료: {migrated}개 항목 변환")


def apply_user_id_mapping(wb) -> int:
    """워크북 내 uid 컬럼(col4)을 기준으로 이름 컬럼(col6)을 채움
    (형식 변경 후: col4=참석자ID, col6=참석자이름)"""
    mapping = _load_id_to_name_mapping()  # {uid: name}
    if not mapping:
        return 0
    # Boss RAW 새 형식: col4=참석자ID(uid), col6=참석자이름
    uid_col, name_col = 4, 6
    updated = 0
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        for row_num in range(2, ws.max_row + 1):
            uid_val = ws.cell(row=row_num, column=uid_col).value
            if uid_val and str(uid_val) in mapping:
                cell = ws.cell(row=row_num, column=name_col)
                new_name = mapping[str(uid_val)]
                if cell.value != new_name:
                    cell.value = new_name
                    updated += 1
    return updated


# === 로그 설정 ===
log = logging.getLogger("BossBot")
log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
log.addHandler(_console)

_file = DailyFileHandler(LOG_DIR, "RaidBook_boss_Bot")
_file.setFormatter(_formatter)
log.addHandler(_file)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

ALERT_CHANNEL_ID = config["alert_channel_id"]
COMMAND_CHANNEL_ID = config.get("command_channel_id", ALERT_CHANNEL_ID)
LOOT_CHANNEL_ID = config.get("loot_channel_id", COMMAND_CHANNEL_ID)
# 슬래시 명령 사용 허용 역할 (서버 관리 권한 보유자는 항상 허용)
ADMIN_ROLE_IDS = {int(r) for r in (config.get("admin_role_ids") or []) if r}
# 음성채널 인원체크 대상 채널 (비우면 서버의 모든 음성채널, AFK 채널 제외)
VOICE_CHANNEL_IDS = [int(c) for c in (config.get("voice_channel_ids") or []) if c]
# 보스 출현 정각에 음성채널 접속자를 자동 참석 처리
VOICE_CHECK_AT_SPAWN = bool(config.get("voice_check_at_spawn", True))
# 알림 메시지의 참석/취소 버튼 숨김 (참석자는 인원체크 버튼·명령으로만 음성채널에서 수집)
HIDE_ATTEND_BUTTONS = bool(config.get("hide_attend_buttons", False))
# 마감 시 비활성화되는 참석 계열 버튼
ATTEND_BUTTON_IDS = ("boss_join", "boss_cancel", "boss_voicecheck")
# 득템 채널 공지 — 💎 득템·경매(진행판)를 쓰면 기록·결과가 알림 메시지에 남으므로 기본 꺼짐
LOOT_ANNOUNCE = bool(config.get("loot_announce", not config.get("loot_auction_button", False)))
# 보스 알림의 💎 득템·경매 — 아이템 입력 한 번으로 득템 기록 + 경매 진행판 자동 진행 (RaidBook_boss_auction)
LOOT_AUCTION_BUTTON = bool(config.get("loot_auction_button", False))
# 구버전 방식: 전리품 공지 메시지의 🔨 경매시작 버튼 → 세팅 입력 → ▶️ 시작 (기본 꺼짐)
LOOT_AUCTION_LEGACY = bool(config.get("loot_auction_legacy", False))
# 진행판 기본값 "시작가 호가 카운트" (입력 창에 미리 채워짐)
AUCTION_DEFAULTS = parse_board_defaults(config.get("auction_defaults", ""))
AUCTION_CHANNEL_ID = int(config.get("auction_channel_id") or 0)
AUCTION_CURRENCY = str(config.get("auction_currency", "아데나") or "")
# 설정된 채널이 없거나 삭제됐으면 시작 시 "RaidBook" 카테고리에 자동 생성 (채널 관리 권한 필요)
AUTO_CREATE_CHANNELS = bool(config.get("auto_create_channels", True))
# 참석자가 아니어도 획득 아이템 버튼을 사용할 수 있는 강제 허용 UID 목록 (관리자/대리 입력 등)
LOOT_FORCE_USER_IDS = set(str(u) for u in (config.get("loot_force_user_ids") or []))

TOKEN = config["token"]
daily_refresh_hour = config.get("daily_refresh_hour", 0)
daily_refresh_minute = config.get("daily_refresh_minute", 0)

# 멀티 보스: { "보스명": {"interval_hours": N, "start_hour": H, "start_minute": M} }
BOSSES_CONFIG = config.get("bosses", {})

# 하위호환: 기존 단일 보스 config 지원
if not BOSSES_CONFIG and "boss_name" in config:
    BOSSES_CONFIG[config["boss_name"]] = {
        "interval_hours": config.get("boss_interval_hours", 2),
        "start_hour": config.get("boss_start_hour", 17),
        "start_minute": config.get("boss_start_minute", 0),
    }

# 매일 고정시간 반복 보스: { "보스명": {"times": ["06:00","12:00"], "alert_minutes": 20} }
DAILY_BOSSES_CONFIG = config.get("daily_bosses", {})
# 보스 출현 N분 전 알림 기본값
DEFAULT_ALERT_MINUTES = config.get("alert_minutes", 20)

# 런타임 보스 상태: { "보스명": {"task": Task, "interval_hours": N} }
boss_states = {}

# 공용 스케줄 레지스트리 — 전투봇이 겹침 판정에 사용 (실제 실행 중인 스케줄만 게시)
_SCHED_REGISTRY_PATH = os.path.join(DATA_DIR, "schedule_registry.json")


def _publish_boss_schedule(name, spec):
    rb_schedule.publish_schedule(_SCHED_REGISTRY_PATH, "boss", name, spec, log)


def _remove_boss_schedule(name):
    rb_schedule.remove_schedule_entry(_SCHED_REGISTRY_PATH, "boss", name, log)
# 매일 고정시간 런타임 상태: { "보스명": {"HH:MM": Task, ...} }
boss_daily_states = {}
daily_refresh_task = None
on_ready_initialized = False
on_ready_initializing = False

EXCEL_PATH = os.path.join(DATA_DIR, "RaidBook_Boss_Data.xlsx")
ACTIVE_MESSAGES_PATH = os.path.join(DATA_DIR, "active_messages.json")
active_messages_lock = threading.RLock()

HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
GRAY_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin")
)

WORKBOOK_SCHEMA_NAME = "boss"
WORKBOOK_SCHEMA_VERSION = 1
SYSTEM_SHEETS = ["히스토리", "보스별 참석 현황", "누적 통계", "획득 아이템", "_meta"]
excel_lock = asyncio.Lock()


def reorder_sheets(wb):
    """시트 순서: 누적 통계, 보스별 참석 현황, 획득 아이템, 히스토리, 날짜별 탭(최신순)"""
    desired_order = []
    for name in ["누적 통계", "보스별 참석 현황", "획득 아이템", "히스토리"]:
        if name in wb.sheetnames:
            desired_order.append(name)
    date_sheets = sorted(
        [s for s in wb.sheetnames if s not in SYSTEM_SHEETS],
        reverse=True
    )
    desired_order.extend(date_sheets)
    wb._sheets = [wb[name] for name in desired_order]


def save_active_message(message_id, channel_id, boss_name, boss_time, disable_time_str,
                        created_date=None, attendees=None, loot="", image_url="", image_filename="",
                        loot_announcement_id=""):
    """활성 메시지 정보를 JSON에 저장"""
    with active_messages_lock:
        data = load_active_messages()
        data[str(message_id)] = {
            "channel_id": channel_id,
            "boss_name": boss_name,
            "boss_time": boss_time,
            "disable_time": disable_time_str,
            "created_date": created_date or datetime.now().strftime("%Y-%m-%d"),
            "attendees": attendees or [],
            "loot": loot or "",
            "loot_announcement_id": str(loot_announcement_id or ""),
            "image_url": image_url or "",
            "image_filename": image_filename or "",
            "party_confirmed": False,
            "confirmed_attendees": [],
            "confirmed_at": "",
        }
        write_active_messages(data)


def update_active_message_attendees(message_id, attendees):
    """활성 메시지의 참석자 목록만 갱신"""
    with active_messages_lock:
        data = load_active_messages()
        key = str(message_id)
        if key in data:
            data[key]["attendees"] = list(attendees)
            write_active_messages(data)


def _upsert_active_message_attendees(message_id, attendees, *, boss_name="", boss_time="",
                                     created_date=None, disable_time=None, channel_id=None,
                                     loot="", image_url="", image_filename="", loot_announcement_id=""):
    """참석자 갱신 — JSON에 키가 없으면(auto_close 후 삭제) 재등록해 재시작 시 유실 방지."""
    with active_messages_lock:
        data = load_active_messages()
        key = str(message_id)
        if key in data:
            data[key]["attendees"] = list(attendees)
            if image_url:
                data[key]["image_url"] = image_url
            if image_filename:
                data[key]["image_filename"] = image_filename
            if loot_announcement_id:
                data[key]["loot_announcement_id"] = str(loot_announcement_id)
        else:
            # auto_close 후 remove_active_message로 키가 삭제된 경우 복원
            from RaidBook_common import write_json_atomic as _wja
            disable_str = ""
            if disable_time:
                try:
                    disable_str = disable_time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            data[key] = {
                "channel_id": channel_id or 0,
                "boss_name": boss_name or "",
                "boss_time": boss_time or "",
                "disable_time": disable_str,
                "created_date": created_date or datetime.now().strftime("%Y-%m-%d"),
                "attendees": list(attendees),
                "loot": loot or "",
                "loot_announcement_id": str(loot_announcement_id or ""),
                "image_url": image_url or "",
                "image_filename": image_filename or "",
            }
            log.info(f"[참석자 upsert] {key} — JSON에 없어 재등록 (boss_name={boss_name!r})")
        write_active_messages(data)


def update_active_message_loot(message_id, loot, loot_announcement_id=None):
    """활성 메시지의 획득 아이템 텍스트 갱신 (마감 이후엔 JSON 에 없으므로 no-op)"""
    with active_messages_lock:
        data = load_active_messages()
        key = str(message_id)
        if key in data:
            data[key]["loot"] = loot or ""
            if loot_announcement_id is not None:
                data[key]["loot_announcement_id"] = str(loot_announcement_id or "")
            write_active_messages(data)


def update_active_message_loot_announcement(message_id, announcement_id):
    """활성 메시지 상태에 득템 채널 공지 메시지 ID를 저장."""
    with active_messages_lock:
        data = load_active_messages()
        key = str(message_id)
        if key in data:
            data[key]["loot_announcement_id"] = str(announcement_id or "")
            write_active_messages(data)


def remove_active_message(message_id):
    """마감된 메시지를 JSON에서 제거"""
    with active_messages_lock:
        data = load_active_messages()
        data.pop(str(message_id), None)
        write_active_messages(data)


def load_active_messages():
    """활성 메시지 목록 로드"""
    with active_messages_lock:
        if not os.path.exists(ACTIVE_MESSAGES_PATH):
            return {}
        try:
            with open(ACTIVE_MESSAGES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.info(f"[활성 메시지 로드 실패] {e}")
            return {}


def write_active_messages(data):
    """활성 메시지를 임시 파일에 쓴 뒤 원자적으로 교체."""
    write_json_atomic(ACTIVE_MESSAGES_PATH, data, log)


def upsert_active_message_info(message_id, info):
    with active_messages_lock:
        data = load_active_messages()
        data[str(message_id)] = dict(info)
        write_active_messages(data)


async def save_workbook_safely(wb, path, label="엑셀"):
    ensure_workbook_meta(wb, WORKBOOK_SCHEMA_NAME, WORKBOOK_SCHEMA_VERSION)
    return await _save_workbook_safely(wb, path, label, logger=log)


def save_workbook_safely_sync(wb, path, label="엑셀"):
    ensure_workbook_meta(wb, WORKBOOK_SCHEMA_NAME, WORKBOOK_SCHEMA_VERSION)
    return _save_workbook_safely_sync(wb, path, label, logger=log)


def backup_excel():
    """당일 롤링 백업. Data/Backup/Boss/ 폴더에 저장, 최근 10개만 유지.
    잦은 버튼 클릭으로 인한 빈번한 호출에도 자정 일자별 스냅샷에는 영향 없음."""
    if not os.path.exists(EXCEL_PATH):
        return
    if not _is_valid_xlsx(EXCEL_PATH):
        log.info(f"[백업 보류] {EXCEL_PATH} 가 손상 상태라 백업하지 않음")
        return

    do_rolling_backup(EXCEL_PATH, BOSS_BACKUP_DIR, "RaidBook_Boss_Data", log)


def backup_excel_daily_snapshot():
    """자정 일자별 스냅샷. Data/Backup/Boss/Daily/ 에 1일 1파일로 저장. 최대 30일 보관."""
    if not os.path.exists(EXCEL_PATH):
        return
    if not _is_valid_xlsx(EXCEL_PATH):
        log.info(f"[일자 백업 보류] {EXCEL_PATH} 손상 상태")
        return
    do_daily_snapshot(EXCEL_PATH, BOSS_BACKUP_DAILY_DIR, "RaidBook_Boss_Data", log)


def restore_backup():
    """복구는 당일 롤링(최신) → 일자별 스냅샷(최신) → 레거시 Data/Backup/ 순으로 시도."""
    restore_latest_backup(
        EXCEL_PATH,
        (BOSS_BACKUP_DIR, BOSS_BACKUP_DAILY_DIR, BACKUP_DIR),
        "RaidBook_Boss_Data",
        log,
    )


def apply_header(ws, headers):
    _apply_header_common(ws, headers, fill=HEADER_FILL, font=HEADER_FONT, border=BORDER)


def auto_column_width(ws):
    _auto_column_width_common(ws)


def apply_border_all(ws):
    _apply_border_all_common(ws, border=BORDER)


def set_id_as_text(ws):
    for row in ws.iter_rows(min_row=2):
        cell = row[0]
        if cell.value:
            cell.value = str(cell.value)
            cell.number_format = '@'
            cell.alignment = Alignment(horizontal="center", vertical="center")


RAW_HEADERS = ["메시지 ID", "보스명", "보스 출현 시간", "참석자ID", "참석 시각", "참석자이름", "획득포인트"]
LOOT_SHEET = "획득 아이템"


def calculate_points(battle_name: str, battle_time: str) -> float:
    """참여 보상 포인트 계산.
    전투 종류 기준: 커츠/도펠=2.0, 여왕개미/여왕거미=1.5, 기타=1.0
    취약시간 기준: 03:00~09:00 참여 시 2.0
    결과 = max(type_pts, time_pts) — 취약시간 참여는 최소 2점 보장
    """
    name_lower = (battle_name or "").lower().strip()

    # 전투 종류 포인트
    if any(k in name_lower for k in ("커츠", "도펠")):
        type_pts = 2.0
    elif any(k in name_lower for k in ("여왕개미", "여왕거미")):
        type_pts = 1.5
    else:
        type_pts = 1.0

    # 취약시간(03:00~09:59) 포인트 — 09시 보스탐 포함
    time_pts = 0.0
    bt = str(battle_time or "").strip()
    if bt:
        try:
            # "HH:MM" 또는 "YYYY-MM-DD HH:MM(:SS)" 형식 모두 지원
            time_part = bt.split(" ")[-1]   # 날짜+시각이면 시각 부분만 추출
            hour = int(time_part.split(":")[0])
            if 3 <= hour <= 9:
                time_pts = 2.0
        except Exception:
            pass

    return max(type_pts, time_pts)
LOOT_HEADERS = ["메시지 ID", "보스명", "보스 출현 시간", "획득 아이템", "마지막 수정자", "마지막 수정 시각", "득템 메시지 ID"]


def reapply_raw_sheet_format(ws):
    apply_header(ws, RAW_HEADERS)
    set_id_as_text(ws)
    apply_border_all(ws)
    auto_column_width(ws)


def get_all_raw_data(wb):
    all_data = {}
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row[0]:
                continue
            boss_name = row[1] if len(row) > 1 else ""
            boss_time = row[2] if len(row) > 2 else ""
            attendee = row[3] if len(row) > 3 else None
            key = (sheet_name, str(boss_name), str(boss_time), str(row[0]))
            if key not in all_data:
                all_data[key] = []
            if attendee and attendee not in all_data[key]:
                all_data[key].append(attendee)
    return all_data


def fix_missing_ids(wb, message_map=None):
    """message_map: {(boss_name, boss_time): message_id} — Discord에서 조회한 매핑"""
    fixed_count = 0
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        date_str = sheet_name.replace("-", "")
        counter = 1
        for row in ws.iter_rows(min_row=2):
            cell_id = row[0]
            if not cell_id.value or str(cell_id.value).strip() == "":
                # Discord 메시지 ID 매칭 시도
                boss_name = str(row[1].value) if len(row) > 1 and row[1].value else ""
                boss_time = str(row[2].value) if len(row) > 2 and row[2].value else ""
                matched_id = message_map.get((boss_name, boss_time)) if message_map else None

                if matched_id:
                    cell_id.value = str(matched_id)
                    log.info(f"[ID 매칭] {boss_name} {boss_time} → 메시지 ID: {matched_id}")
                else:
                    new_id = f"{date_str}-{counter:03d}"
                    while any(
                        str(r[0]) == new_id
                        for r in ws.iter_rows(min_row=2, values_only=True)
                        if r[0]
                    ):
                        counter += 1
                        new_id = f"{date_str}-{counter:03d}"
                    cell_id.value = new_id
                    counter += 1

                cell_id.number_format = '@'
                fixed_count += 1
    return fixed_count


async def build_message_map_from_channel(days=7):
    """Discord 채널에서 (boss_name, boss_time) → message_id 매핑 구축"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return {}

    message_map = {}
    cutoff = datetime.now() - timedelta(days=days)

    async for msg in channel.history(limit=500, after=cutoff):
        if msg.author.id != bot.user.id:
            continue
        if not msg.embeds:
            continue

        embed = msg.embeds[0]
        desc = embed.description or ""
        title = embed.title or ""
        if "보스 출현 알림" not in f"{title}\n{desc}":
            continue

        boss_name = ""
        boss_time = ""
        if "보스 출현 알림" in title:
            boss_name = title.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
        for line in desc.split("\n"):
            if "보스 출현 알림" in line:
                boss_name = line.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
            elif "보스 리젠 시각" in line:
                boss_time = line.split("**")[-2].strip() if "**" in line else ""

        if boss_name and boss_time:
            message_map[(boss_name, boss_time)] = str(msg.id)

    return message_map


def auto_migrate_excel(wb):
    """RAW_HEADERS와 시트 헤더를 비교하여 컬럼 불일치 시 자동 보정"""
    migrated = False
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        current_headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]

        if current_headers == RAW_HEADERS:
            continue

        # 현재 헤더 → 새 헤더 매핑 (이름 기준)
        old_to_col = {}
        for idx, h in enumerate(current_headers):
            if h:
                old_to_col[h] = idx

        # 기존 데이터 읽기
        rows_data = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            rows_data.append(list(row))

        # 시트 초기화
        for row in ws.iter_rows(min_row=1):
            for cell in row:
                cell.value = None

        apply_header(ws, RAW_HEADERS)

        # 데이터 재배치
        for row_idx, old_row in enumerate(rows_data, start=2):
            for new_col_idx, new_header in enumerate(RAW_HEADERS):
                if new_header in old_to_col:
                    old_col_idx = old_to_col[new_header]
                    if old_col_idx < len(old_row):
                        ws.cell(row=row_idx, column=new_col_idx + 1, value=old_row[old_col_idx])

        reapply_raw_sheet_format(ws)
        migrated = True
        log.info(f"[자동 마이그레이션] {sheet_name}: {current_headers} → {RAW_HEADERS}")

    return migrated


def migrate_old_excel(wb, default_boss_name):
    """기존 4컬럼(메시지ID,보스시간,참석자,참석시각) → 5컬럼(메시지ID,보스명,보스시간,참석자,참석시각) 마이그레이션"""
    migrated_count = 0
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        # 헤더로 이미 마이그레이션 됐는지 판별
        header_b = ws.cell(row=1, column=2).value
        if header_b == "보스명":
            continue

        # 뒤에서부터 2번째 컬럼에 삽입
        ws.insert_cols(2)
        ws.cell(row=1, column=2, value="보스명")

        for row_idx in range(2, ws.max_row + 1):
            ws.cell(row=row_idx, column=2, value=default_boss_name)
            migrated_count += 1

        reapply_raw_sheet_format(ws)

    return migrated_count


def _ensure_loot_sheet(wb):
    """획득 아이템 시트가 없으면 생성하고 헤더 적용 후 반환."""
    return _ensure_loot_sheet_core(
        wb,
        sheet_name=LOOT_SHEET,
        headers=LOOT_HEADERS,
        apply_header=apply_header,
    )


def load_loot_from_excel(message_id):
    """Excel '획득 아이템' 시트에서 메시지 ID 매칭 → (loot, editor, announcement_msg_id) 반환. 없으면 ('', '', '')."""
    return _load_loot_record_core(EXCEL_PATH, message_id, loot_sheet=LOOT_SHEET)


def load_loot_acquirers_from_excel(message_id):
    """획득자(8번째 컬럼) 문자열 반환. 없으면 ''."""
    return _load_loot_acquirers_core(EXCEL_PATH, message_id, loot_sheet=LOOT_SHEET)


def load_expired_message_bundle(message_id):
    """마감 메시지 클릭 시 필요한 데이터를 워크북 1회 로드로 일괄 반환.

    반환: dict(attendees[str], boss_name, boss_time, loot, loot_acquirers, loot_announcement_id)
    기존에는 load_attendees/name/time/loot 를 각각 호출해 워크북을 4~5회 로드했고
    총 4초를 넘겨 Discord 3초 인터랙션 응답 시한을 초과했다. (획득아이템 창이 안 뜨는 원인)
    """
    result = {
        "attendees": [],
        "boss_name": "",
        "boss_time": "",
        "loot": "",
        "loot_acquirers": "",
        "loot_announcement_id": "",
    }
    if not os.path.exists(EXCEL_PATH):
        return result
    target = str(message_id)
    # JSON 미러 우선 (7단계) — 실패 시 Excel 직접 읽기
    try:
        from RaidBook_boss_data import _mirror as _bd_mirror
        m = _bd_mirror(EXCEL_PATH, system_sheets=SYSTEM_SHEETS)
        if m is not None:
            for _sn, recs in m.iter_sheets(system_sheets=SYSTEM_SHEETS):
                for rec in recs:
                    if rec.get("msg_id") != target:
                        continue
                    if not result["boss_name"] and rec.get("event"):
                        result["boss_name"] = str(rec["event"])
                    if not result["boss_time"] and rec.get("event_time"):
                        result["boss_time"] = str(rec["event_time"])
                    uid = rec.get("uid")
                    if uid and str(uid) not in result["attendees"]:
                        result["attendees"].append(str(uid))
            entry = m.loot_entry(target)
            if entry:
                result["loot"] = entry.get("loot_text", "")
                result["loot_announcement_id"] = entry.get("ann_id", "")
                result["loot_acquirers"] = entry.get("acquirers", "")
            return result
    except Exception as e:
        log.info(f"[만료 메시지 미러 실패 → Excel 폴백] {target}: {e}")
    try:
        wb = load_workbook(EXCEL_PATH, read_only=True)
    except Exception:
        return result
    try:
        for sheet_name in wb.sheetnames:
            if sheet_name in SYSTEM_SHEETS:
                continue
            ws = wb[sheet_name]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0] or str(row[0]) != target:
                    continue
                if not result["boss_name"] and len(row) > 1 and row[1]:
                    result["boss_name"] = str(row[1])
                if not result["boss_time"] and len(row) > 2 and row[2] is not None:
                    bt = row[2]
                    result["boss_time"] = bt.strftime("%H:%M") if hasattr(bt, "strftime") else str(bt)
                attendee = row[3] if len(row) > 3 else None
                if attendee is not None and str(attendee) not in result["attendees"]:
                    result["attendees"].append(str(attendee))
        if LOOT_SHEET in wb.sheetnames:
            ws = wb[LOOT_SHEET]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0] or str(row[0]) != target:
                    continue
                result["loot"] = str(row[3]) if len(row) > 3 and row[3] else ""
                result["loot_announcement_id"] = str(row[6]) if len(row) > 6 and row[6] else ""
                result["loot_acquirers"] = str(row[7]) if len(row) > 7 and row[7] else ""
                break
    finally:
        wb.close()
    return result


async def save_boss_loot_to_excel(message_id, boss_name, boss_time, loot_text, editor, announcement_msg_id=None, acquirers=None):
    """획득 아이템 시트에 한 줄 upsert (메시지 ID 기준).
    announcement_msg_id 가 None 이면 기존 값 보존, 값이 주어지면 갱신.
    반환: (성공 여부, 기존 announcement_msg_id) — 호출자가 새로 게시할지 수정할지 판단 용도."""
    async with excel_lock:
        try:
            if not os.path.exists(EXCEL_PATH):
                wb = openpyxl.Workbook()
                # 기본 시트 제거
                default = wb.active
                wb.remove(default)
            else:
                wb = load_workbook(EXCEL_PATH)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            existing_ann = _upsert_loot_record_core(
                wb,
                message_id,
                boss_name,
                boss_time,
                loot_text,
                editor,
                announcement_msg_id=announcement_msg_id,
                now_str=now_str,
                acquirers=acquirers,
                loot_sheet=LOOT_SHEET,
                loot_headers=LOOT_HEADERS,
                apply_header=apply_header,
            )
            ws = wb[LOOT_SHEET]

            apply_border_all(ws)
            auto_column_width(ws)
            reorder_sheets(wb)

            save_ok, last_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
            if save_ok:
                backup_excel()
                log.info(f"[획득아이템 저장] {boss_name} {boss_time} | 메시지 {message_id} | 편집자: {editor} | 아이템: {loot_text[:60]}")
                return True, existing_ann
            else:
                log.info(f"[획득아이템 저장 실패] {boss_name} {boss_time} | 메시지 {message_id} | {last_err}")
                return False, existing_ann
        except Exception as e:
            log.info(f"[획득아이템 저장 오류] {e}")
            return False, ""


async def save_boss_attendees_to_excel(message_id, boss_name, boss_time, attendees, created_date=None):
    """마감 시 참석자 목록을 한 번에 Excel에 저장"""
    async with excel_lock:
        today = created_date or datetime.now().strftime("%Y-%m-%d")
        try:
            wb = openpyxl.Workbook() if not os.path.exists(EXCEL_PATH) else load_workbook(EXCEL_PATH)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            mapping = _load_id_to_name_mapping()  # {uid: name}
            ws = _upsert_attendance_records_core(
                wb,
                today,
                message_id,
                boss_name,
                boss_time,
                attendees,
                id_to_name=mapping,
                calculate_points=calculate_points,
                raw_headers=RAW_HEADERS,
                apply_header=apply_header,
                now_str=now_str,
            )

            apply_border_all(ws)
            auto_column_width(ws)

            update_history_sheet(wb)
            update_boss_attendance_sheet(wb)
            update_stats_sheet(wb)
            reorder_sheets(wb)

            save_ok, last_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")

            if save_ok:
                # (B) 성공 시점에만 백업해 백업 풀이 항상 유효 상태로 유지
                backup_excel()
                log.info(f"[마감 저장] {boss_name} {boss_time} | 참석자: {', '.join(attendees) if attendees else '없음'}")
            else:
                # (C) 최종 실패 시 디스코드 알림 — restore_backup은 호출하지 않음 (디스크 파일은 미변경)
                log.info(f"[엑셀 저장 실패] {boss_name} {boss_time} | {last_err} | 재시도 4회 실패 — 엑셀 파일이 열려있는지 확인")
                try:
                    ch = bot.get_channel(COMMAND_CHANNEL_ID)
                    if ch:
                        await ch.send(
                            f"⚠️ **엑셀 저장 실패** — `RaidBook_Boss_Data.xlsx` 가 열려있어 `{boss_name} {boss_time}` 마감 데이터가 반영되지 않았어요.\n"
                            f"엑셀 앱을 닫고 `/refresh` 를 실행해 주세요. (참석자 데이터는 로그에 남아있어 복구 가능)"
                        )
                except Exception:
                    pass

        except Exception as e:
            log.info(f"[엑셀 저장 오류] {e}")


def update_history_sheet(wb):
    all_data = get_all_raw_data(wb)
    if "히스토리" in wb.sheetnames:
        del wb["히스토리"]
    ws = wb.create_sheet("히스토리")
    apply_header(ws, ["날짜", "보스명", "보스 출현 시간", "메시지 ID", "참석자 수", "참석자 목록"])
    for (date, boss_name, boss_time, msg_id), attendees in sorted(all_data.items()):
        next_row = ws.max_row + 1
        ws.cell(row=next_row, column=1, value=date)
        ws.cell(row=next_row, column=2, value=boss_name)
        ws.cell(row=next_row, column=3, value=boss_time)
        ws.cell(row=next_row, column=4, value=str(msg_id)).number_format = '@'
        ws.cell(row=next_row, column=5, value=len(attendees))
        ws.cell(row=next_row, column=6, value=", ".join(attendees))
    apply_border_all(ws)
    auto_column_width(ws)


def update_boss_attendance_sheet(wb):
    all_data = get_all_raw_data(wb)
    if not all_data:
        return
    all_attendees = sorted(set(att for attendees in all_data.values() for att in attendees))
    if "보스별 참석 현황" in wb.sheetnames:
        del wb["보스별 참석 현황"]
    ws = wb.create_sheet("보스별 참석 현황")
    headers = ["날짜", "보스명", "보스 출현 시간", "참석 인원"] + all_attendees
    apply_header(ws, headers)
    for (date, boss_name, boss_time, msg_id), attendees in sorted(all_data.items()):
        row_data = [date, boss_name, boss_time, len(attendees)]
        for att in all_attendees:
            row_data.append("✅" if att in attendees else "❌")
        ws.append(row_data)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if cell.value == "✅":
                cell.fill = GREEN_FILL
            elif cell.value == "❌":
                cell.fill = GRAY_FILL
    auto_column_width(ws)


def update_stats_sheet(wb):
    # 날짜별 시트 수집 (최신순 정렬)
    date_sheets = sorted(
        [s for s in wb.sheetnames if s not in SYSTEM_SHEETS],
        reverse=True
    )
    if not date_sheets:
        return

    # 참석자별 { 전체누적, 날짜별 횟수 } 집계
    attendee_total = defaultdict(int)
    attendee_by_date = defaultdict(lambda: defaultdict(int))

    for sheet_name in date_sheets:
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=2, values_only=True):
            attendee = row[3] if len(row) > 3 else None
            if not attendee:
                continue
            attendee_total[attendee] += 1
            attendee_by_date[attendee][sheet_name] += 1

    if not attendee_total:
        return

    if "누적 통계" in wb.sheetnames:
        del wb["누적 통계"]
    ws = wb.create_sheet("누적 통계")

    # 헤더: 참석자 | 전체 누적 | 날짜1 | 날짜2 | ...
    headers = ["참석자", "전체 누적"] + date_sheets
    apply_header(ws, headers)

    sorted_attendees = sorted(attendee_total.items(), key=lambda x: x[1], reverse=True)
    max_total = sorted_attendees[0][1] if sorted_attendees else 1

    for attendee, total in sorted_attendees:
        row_data = [attendee, total]
        for date in date_sheets:
            row_data.append(attendee_by_date[attendee].get(date, 0))
        ws.append(row_data)

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if cell.column >= 2 and cell.value and isinstance(cell.value, (int, float)):
                if cell.value > 0:
                    intensity = int(255 - (cell.value / max_total) * 150)
                    hex_color = f"{intensity:02X}FF{intensity:02X}"
                    cell.fill = PatternFill(start_color=hex_color, end_color=hex_color, fill_type="solid")
    auto_column_width(ws)


def load_attendees_from_excel(message_id):
    return _load_attendees_by_message_id_core(
        EXCEL_PATH,
        message_id,
        system_sheets=SYSTEM_SHEETS,
    )


def load_boss_time_from_excel(message_id):
    if not os.path.exists(EXCEL_PATH):
        return None
    wb = load_workbook(EXCEL_PATH)
    str_message_id = str(message_id)
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if str(row[0]) == str_message_id:
                return row[2] if len(row) > 2 else None
    return None


def load_boss_name_from_excel(message_id):
    if not os.path.exists(EXCEL_PATH):
        return None
    wb = load_workbook(EXCEL_PATH)
    str_message_id = str(message_id)
    for sheet_name in wb.sheetnames:
        if sheet_name in SYSTEM_SHEETS:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if str(row[0]) == str_message_id:
                return row[1] if len(row) > 1 else None
    return None


def find_message_id_by_datetime(date_time_str):
    """날짜+시간 문자열(YYMMDDHHMM)로 메시지 ID 검색. 예: 2605161700 → 2026-05-16 시트에서 17:00 검색"""
    if len(date_time_str) != 10:
        return None, None, None
    try:
        date_str = f"20{date_time_str[:2]}-{date_time_str[2:4]}-{date_time_str[4:6]}"
        time_str = f"{date_time_str[6:8]}:{date_time_str[8:10]}"
    except Exception:
        return None, None, None

    if not os.path.exists(EXCEL_PATH):
        return None, None, None
    wb = load_workbook(EXCEL_PATH)
    if date_str not in wb.sheetnames:
        return None, None, None
    ws = wb[date_str]
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[2] and str(row[2]) == time_str:
            return str(row[0]), row[1], row[2]
    return None, None, None


def get_history_text(days=7):
    if not os.path.exists(EXCEL_PATH):
        return None
    wb = load_workbook(EXCEL_PATH)
    if "히스토리" not in wb.sheetnames:
        return None
    ws = wb["히스토리"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    if not rows:
        return None
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    filtered = [r for r in rows if r[0] and str(r[0]) >= cutoff]
    if not filtered:
        return None
    grouped = defaultdict(list)
    for row in filtered:
        grouped[row[0]].append(row)
    text = f"📊 **보스탐 히스토리 (최근 {days}일)**\n"
    text += "─" * 30 + "\n"
    for date in sorted(grouped.keys(), reverse=True):
        text += f"\n📅 **{date}**\n"
        for row in grouped[date]:
            text += f"  🐉 {row[1]} | ⏰ {row[2]} | 👥 {row[4]}명 | {row[5]}\n"
    return text


def _coerce_disable_time(value):
    """disable_time 입력을 datetime 으로 통일. str/datetime/None 허용."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
    return None


async def _find_loot_announcement_message(channel, boss_msg_id, existing_announcement_id=None, *, history_limit=300):
    return await _find_loot_announcement_message_common(
        channel,
        boss_msg_id,
        existing_announcement_id,
        bot_user=bot.user,
        logger=log,
        log_prefix="득템 공지",
        source_label="보스",
        history_limit=history_limit,
    )


async def _post_or_update_loot_announcement(
    boss_msg_id,
    boss_name,
    boss_time,
    loot_text,
    editor,
    guild_id,
    existing_announcement_id=None,
    *,
    create_if_missing=True,
    acquirers="",
):
    return await _post_or_update_loot_announcement_common(
        bot=bot,
        loot_channel_id=LOOT_CHANNEL_ID,
        source_channel_id=ALERT_CHANNEL_ID,
        source_message_id=boss_msg_id,
        subject_name=boss_name,
        subject_time=boss_time,
        loot_text=loot_text,
        editor=editor,
        guild_id=guild_id,
        acquirers=acquirers,
        existing_announcement_id=existing_announcement_id,
        create_if_missing=create_if_missing,
        logger=log,
        log_prefix="득템 공지",
        source_label="보스",
        body_line="용사들이 전리품을 획득했습니다!",
        jump_label="알림 메시지로 이동",
        view=LootAuctionView() if LOOT_AUCTION_LEGACY else None,
    )


def _source_msg_id_from_announcement(message):
    """전리품 공지 embed의 '알림 메시지로 이동' 링크에서 원본 보스 알림 메시지ID 추출."""
    for embed in getattr(message, "embeds", []) or []:
        m = re.search(r"discord\.com/channels/\d+/\d+/(\d+)", getattr(embed, "description", "") or "")
        if m:
            return int(m.group(1))
    return None


def _boss_attendees_for(message_id):
    """보스 알림 참석자 UID 목록 — 진행 중이면 active_messages, 마감 후면 Excel."""
    info = load_active_messages().get(str(message_id))
    if info is not None:
        return list(info.get("attendees", []))
    return load_attendees_from_excel(int(message_id)) or []


def _loot_text_from_announcement(message) -> str:
    """전리품 공지 embed의 ``` 블록에서 아이템 텍스트 추출 (경매 물품 기본값)."""
    for embed in getattr(message, "embeds", []) or []:
        desc = getattr(embed, "description", "") or ""
        m = re.search(r"```\n?(.*?)\n?```", desc, re.S)
        if m:
            text = m.group(1).strip()
            return "" if text == "(기록 없음)" else text
    return ""


class LootAuctionView(ui.View):
    """전리품 공지 메시지의 경매시작 버튼 (persistent)."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="경매시작", emoji="🔨", style=discord.ButtonStyle.primary, custom_id="boss_loot_auction")
    async def start_auction(self, interaction: discord.Interaction, button: ui.Button):
        if not _is_bot_admin(interaction.user):
            await interaction.response.send_message("⛔ 경매 시작 권한이 없어요.", ephemeral=True)
            return
        await interaction.response.send_modal(
            AuctionSetupModal(boss_auction, _loot_text_from_announcement(interaction.message),
                              source_message_id=_source_msg_id_from_announcement(interaction.message)))


class LootCountSelectView(discord.ui.View):
    """획득 아이템 개수 선택 → 개별 입력 모달 연결"""
    def __init__(self, parent_view: "AttendView", existing_loot: str = "", opened_revision: int = 0,
                 existing_acquirers: str = ""):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.existing_loot = existing_loot
        self.existing_acquirers = existing_acquirers
        self.opened_revision = opened_revision

        # 기존 아이템 파싱 → 기본 개수 (개별입력 모드는 최대 4개, 초과 시 기타)
        existing_items = [x.strip() for x in re.split(r'[,\n]+', existing_loot) if x.strip()]
        self.count = 0 if len(existing_items) > 4 else max(len(existing_items), 1)

        placeholder = (f"획득한 아이템 개수 선택 (현재 {len(existing_items)}개 등록됨)"
                       if existing_items else "획득한 아이템 개수를 선택하세요")
        options = [
            discord.SelectOption(label=f"{i}개", value=str(i),
                                 default=(i == self.count))
            for i in range(1, 5)
        ] + [discord.SelectOption(label="기타 (직접 입력)", value="0",
                                  description="5개 이상이거나 자유롭게 입력할 때",
                                  default=(self.count == 0))]
        sel = discord.ui.Select(placeholder=placeholder, options=options)
        sel.callback = self._on_select
        self.add_item(sel)

        btn = discord.ui.Button(label="다음 →", style=discord.ButtonStyle.primary)
        btn.callback = self._on_next
        self.add_item(btn)

    async def _on_select(self, interaction: discord.Interaction):
        self.count = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _on_next(self, interaction: discord.Interaction):
        if is_loot_processing(self.parent_view.message_id):
            await interaction.response.send_message(loot_busy_message(), ephemeral=True)
            return
        existing_items = [x.strip() for x in re.split(r'[,\n]+', self.existing_loot) if x.strip()]
        await interaction.response.send_modal(
            LootModal(
                self.parent_view,
                count=self.count,
                prefill_items=existing_items,
                opened_revision=self.opened_revision,
                prefill_acquirers=self.existing_acquirers,
            ))


class LootModal(ui.Modal, title="획득 아이템 기록"):
    """아이템 개수만큼 개별 입력 필드 / 기타(직접입력) 모드 + 획득자 입력.

    Discord 모달은 입력칸 최대 5개라, 개별입력 모드는 아이템 최대 4개 + 획득자 1칸.
    """
    def __init__(self, parent_view: "AttendView", count: int = 1, prefill_items: list = None,
                 opened_revision: int = 0, prefill_acquirers: str = ""):
        super().__init__()
        self.parent_view = parent_view
        self.opened_revision = opened_revision
        prefill_items = prefill_items or []
        self.inputs = []
        self.free_input = None

        if count == 0:
            # 기타 — 자유 입력 (paragraph)
            self.free_input = ui.TextInput(
                label="획득 아이템 (직접 입력)",
                style=discord.TextStyle.paragraph,
                placeholder="쉼표 또는 줄바꿈으로 구분\n예: 브레스 오브 파이어\n선버스트\n기타 아이템",
                required=False,
                max_length=1000,
                default=", ".join(prefill_items) if prefill_items else "",
            )
            self.add_item(self.free_input)
        else:
            for i in range(max(1, min(count, 4))):
                label = f"아이템 {i + 1}" if count > 1 else "아이템"
                inp = ui.TextInput(
                    label=label,
                    style=discord.TextStyle.short,
                    placeholder="아이템 이름을 입력하세요",
                    required=(i == 0),
                    max_length=200,
                    default=prefill_items[i] if i < len(prefill_items) else "",
                )
                self.add_item(inp)
                self.inputs.append(inp)

        # 획득자 입력칸 (1명 ~ 아이템 갯수, 쉼표/줄바꿈 구분)
        self.acquirer_input = ui.TextInput(
            label="획득자 (쉼표 구분, 선택)",
            style=discord.TextStyle.paragraph,
            placeholder="아이템을 획득한 사람. 예: 홍길동, 김철수",
            required=False,
            max_length=500,
            default=prefill_acquirers or "",
        )
        self.add_item(self.acquirer_input)

    async def on_submit(self, interaction: discord.Interaction):
        v = self.parent_view
        if self.free_input:
            # 기타 직접입력 — 쉼표·줄바꿈 파싱
            items = [x.strip() for x in re.split(r'[,\n]+', self.free_input.value or "") if x.strip()]
        else:
            # 개별 입력 필드 수집 (빈 항목 제외)
            items = [inp.value.strip() for inp in self.inputs if inp.value.strip()]
        new_text = ", ".join(items)
        # 획득자 파싱 (쉼표/줄바꿈 구분 → 쉼표 조인)
        acquirers_list = [x.strip() for x in re.split(r'[,\n]+', self.acquirer_input.value or "") if x.strip()]
        new_acquirers = ", ".join(acquirers_list)
        # 응답을 즉시 defer — 이후 작업은 followup으로
        await interaction.response.defer(ephemeral=True)
        ack = await _commit_loot_record(v, interaction, new_text, new_acquirers, self.opened_revision)
        try:
            await interaction.followup.send(ack or loot_busy_message(), ephemeral=True)
        except Exception:
            pass


async def _commit_loot_record(v, interaction, new_text, new_acquirers, opened_revision):
    """획득 아이템 기록 공통 처리 — 알림 embed 갱신·Excel 저장·득템 채널 공지.
    호출 전에 interaction 응답(defer)을 끝내야 한다. 반환: 안내문 (다른 사람이 동시에 수정 중이면 None)."""
    if get_loot_revision(v.message_id) != opened_revision:
        return None
    if not begin_loot_processing(v.message_id):
        return None

    editor = interaction.user.display_name
    guild_id = interaction.guild_id
    should_bump_revision = False
    old_loot = ""
    existing_ann = ""
    loot_changed = False
    try:
        # 원본 보스 알림 메시지 fetch (ephemeral 경유 시 interaction.message가 다를 수 있음)
        boss_msg = None
        try:
            ch = bot.get_channel(v.channel_id) if hasattr(v, 'channel_id') and v.channel_id else interaction.channel
            if ch and v.message_id:
                boss_msg = await ch.fetch_message(int(v.message_id))
        except Exception:
            pass

        async with v._view_lock:
            # 기존 공지 메시지 ID 및 기존 아이템 텍스트 확인.
            # lock 안에서 읽어야 동시/중복 submit이 직전 저장값을 기준으로 판정된다.
            old_loot, _, excel_ann = load_loot_from_excel(v.message_id)
            active_info = load_active_messages().get(str(v.message_id), {})
            state_ann = (
                str(getattr(v, "loot_announcement_id", "") or "")
                or str(active_info.get("loot_announcement_id", "") or "")
                or str(active_info.get("announcement_msg_id", "") or "")
            )
            existing_ann = state_ann or (excel_ann or "")
            if existing_ann:
                v.loot_announcement_id = str(existing_ann)
            old_acquirers = (load_loot_acquirers_from_excel(v.message_id) or "").strip()
            loot_changed = (new_text != (old_loot or "").strip()) or (new_acquirers != old_acquirers)
            v.loot = new_text
            v.loot_acquirers = new_acquirers

            was_active = not getattr(v, "expired", False)

            # ── 1단계: 활성 메시지 처리 ──────────────────────────────────────
            if was_active:
                try:
                    v.attendees = v._verify_attendees_from_log()
                except Exception as e:
                    log.info(f"[획득아이템 참석자 검증 실패] {e}")
                # 메시지는 활성 유지 — embed에 loot만 반영 (Excel 저장보다 먼저 → 화면 반응 우선)
                _id_to_name = _load_id_to_name_mapping()
                names = ", ".join(_id_to_name.get(u, u) for u in v.attendees) if v.attendees else "없음"
                try:
                    target_msg = boss_msg or interaction.message
                    image_filename, image_url = _message_image_ref(target_msg)
                    if image_filename:
                        v.image_filename = image_filename
                    if image_url:
                        v.image_url = image_url
                    image_file, image_kwargs = _boss_image_file_and_kwargs(v.boss_name, target_msg)
                    embed = make_attend_embed(v.boss_name, len(v.attendees), names, v.boss_time,
                                              expired=False, loot=new_text, disable_time=v.disable_time,
                                              loot_acquirers=new_acquirers, **image_kwargs)
                    await _edit_message_with_image(
                        target_msg,
                        embed=embed,
                        view=v,
                        image_file=image_file,
                        content=_boss_alert_content(v.boss_name, v.boss_time),
                    )
                except Exception:
                    pass
                # 참석자 Excel 저장 (유실 방지)
                try:
                    await save_boss_attendees_to_excel(
                        v.message_id, v.boss_name, v.boss_time,
                        v.attendees, v.created_date)
                    log.info(f"[획득아이템 저장] {v.boss_name} {v.boss_time} {v.message_id} | "
                             f"참석 {len(v.attendees)}명 Excel 저장")
                except Exception as e:
                    log.info(f"[획득아이템 참석자 저장 실패] {e}")
                # JSON의 loot · attendees 갱신
                try:
                    active = load_active_messages()
                    if str(v.message_id) in active:
                        active[str(v.message_id)]["loot"] = new_text
                        active[str(v.message_id)]["loot_acquirers"] = new_acquirers
                        active[str(v.message_id)]["attendees"] = list(v.attendees)
                        active[str(v.message_id)]["loot_announcement_id"] = getattr(v, "loot_announcement_id", "") or ""
                        if getattr(v, "image_url", ""):
                            active[str(v.message_id)]["image_url"] = v.image_url
                        if getattr(v, "image_filename", ""):
                            active[str(v.message_id)]["image_filename"] = v.image_filename
                        write_active_messages(active)
                except Exception:
                    pass
            else:
                # 이미 마감된 메시지 — embed에 loot만 반영
                _id_to_name = _load_id_to_name_mapping()
                names = ", ".join(_id_to_name.get(u, u) for u in v.attendees) if v.attendees else "없음"
                try:
                    target_msg = boss_msg or interaction.message
                    image_filename, image_url = _message_image_ref(target_msg)
                    if image_filename:
                        v.image_filename = image_filename
                    if image_url:
                        v.image_url = image_url
                    image_file, image_kwargs = _boss_image_file_and_kwargs(v.boss_name, target_msg)
                    embed = make_attend_embed(v.boss_name, len(v.attendees), names, v.boss_time,
                                              expired=True, loot=new_text, disable_time=v.disable_time,
                                              loot_acquirers=new_acquirers, **image_kwargs)
                    await _edit_message_with_image(
                        target_msg,
                        embed=embed,
                        view=v,
                        image_file=image_file,
                        content=_boss_alert_content(v.boss_name, v.boss_time, expired=True),
                    )
                except Exception:
                    pass

            # ── 2단계: 획득 아이템 Excel 저장 ─────────────────────────────────
            ok, _ = await save_boss_loot_to_excel(v.message_id, v.boss_name, v.boss_time, new_text, editor,
                                                  acquirers=new_acquirers)
            should_bump_revision = ok and loot_changed

        # ── 3단계: 득템 채널 공지 발행/수정/삭제 ──────────────────────────────
        is_loot_update = bool((old_loot or "").strip())
        announcement_id = existing_ann
        # 아이템이 같아도 공지가 없으면(채널 변경·삭제 등) 다시 게시되도록 항상 확인
        if new_text and LOOT_ANNOUNCE:
            announcement_id = await _post_or_update_loot_announcement(
                v.message_id, v.boss_name, v.boss_time, new_text, editor,
                guild_id, existing_announcement_id=existing_ann or None,
                # 기존 공지를 못 찾으면(득템 채널 변경 등) 새로 게시 — 중복은 jump 링크 검색으로 방지
                create_if_missing=True,
                acquirers=new_acquirers,
            )
            if announcement_id and str(announcement_id) != (existing_ann or ""):
                await save_boss_loot_to_excel(
                    v.message_id, v.boss_name, v.boss_time, new_text, editor,
                    announcement_msg_id=announcement_id,
                )
            if announcement_id:
                v.loot_announcement_id = str(announcement_id)
                update_active_message_loot(v.message_id, new_text, announcement_id)
                if str(announcement_id) == (existing_ann or "") and loot_changed:
                    await save_boss_loot_to_excel(
                        v.message_id, v.boss_name, v.boss_time, new_text, editor,
                        announcement_msg_id=announcement_id,
                    )
        elif LOOT_ANNOUNCE and not new_text and loot_changed and (existing_ann or is_loot_update):
            await _delete_loot_announcement_common(
                bot=bot,
                loot_channel_id=LOOT_CHANNEL_ID,
                source_message_id=v.message_id,
                existing_announcement_id=existing_ann or None,
                subject_name=v.boss_name,
                subject_time=v.boss_time,
                logger=log,
                log_prefix="득템 공지",
                source_label="보스",
            )
            await save_boss_loot_to_excel(
                v.message_id, v.boss_name, v.boss_time, "", editor,
                announcement_msg_id="", acquirers="",
            )
            v.loot_announcement_id = ""
            v.loot_acquirers = ""
            update_active_message_loot(v.message_id, "", "")
            announcement_id = None

        log.info(f"[획득아이템 기록] {v.boss_name} {v.boss_time} {v.message_id} | 편집자: {editor} | 내용: {new_text[:80]}")
        # ── 4단계: 완료 응답            # ── 5단계: 완료 응답 ─────────────────────────────────────────────
        if new_text:
            ack = f"✏️ {editor}님, 획득 아이템 기록을 저장했어요."
        else:
            ack = f"🧹 {editor}님, 획득 아이템 기록을 비웠어요."
        if not ok:
            ack += "\n⚠️ 단, 엑셀 저장은 실패했어요. 엑셀이 열려있는지 확인해 주세요."
        if LOOT_ANNOUNCE and new_text and announcement_id is None:
            if is_loot_update:
                ack += "\n⚠️ 기존 득템 채널 공지를 찾지 못해 새 공지는 만들지 않았어요."
            else:
                ack += "\n⚠️ 득템 채널 공지는 실패했어요. (채널 권한/존재 여부 확인)"
        return ack
    finally:
        if should_bump_revision:
            bump_loot_revision(v.message_id)
        end_loot_processing(v.message_id)



def _split_record_items(text):
    return [x.strip() for x in re.split(r"[,\n]+", text or "") if x.strip()]


class LootRecordModal(ui.Modal, title="📦 획득 아이템"):
    """획득 아이템 기록만 (경매는 🔨 경매생성 버튼에서 아이템을 골라 따로 만든다)."""

    def __init__(self, parent_view: "AttendView", opened_revision: int = 0, prefill: str = ""):
        super().__init__()
        self.parent_view = parent_view
        self.opened_revision = opened_revision
        self.items_input = ui.TextInput(
            label="획득 아이템 (쉼표 또는 줄바꿈 구분)",
            style=discord.TextStyle.paragraph, required=False, max_length=1000,
            placeholder="예: 집행검, 반지 x2, 엘릭서 5개",
            default=prefill[:1000],
        )
        self.add_item(self.items_input)

    async def on_submit(self, interaction: discord.Interaction):
        v = self.parent_view
        items = _split_record_items(self.items_input.value)
        await interaction.response.defer(ephemeral=True)
        ack = await _commit_loot_record(v, interaction, ", ".join(items),
                                        getattr(v, "loot_acquirers", "") or "", self.opened_revision)
        if ack is None:
            await interaction.followup.send(loot_busy_message(), ephemeral=True)
            return
        if items and LOOT_AUCTION_BUTTON:
            ack += "\n🔨 경매는 보스 알림의 **경매생성** 버튼에서 아이템을 골라 만들 수 있어요."
        await interaction.followup.send(ack, ephemeral=True)


class ModalOpenView(discord.ui.View):
    """마감된 알림은 데이터 조회에 시간이 걸려 팝업을 바로 못 띄우므로 버튼 한 번 거친다."""

    def __init__(self, make_modal, label="입력창 열기", emoji="📦"):
        super().__init__(timeout=300)
        self.make_modal = make_modal
        self.open_btn.label = label
        self.open_btn.emoji = emoji

    @ui.button(label="입력창 열기", style=discord.ButtonStyle.primary)
    async def open_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(self.make_modal())


async def _apply_board_results(board):
    """진행판이 끝나면 결과를 득템 기록(Excel 획득자 칸)에 남긴다. 공개 보스 알림에는 싣지 않는다."""
    mid = board.get("source_message_id")
    # 재경매로 진행판이 여러 개면 아이템별 최신 결과로 합친다 (앞 낙찰 결과를 덮어쓰지 않도록)
    results = (lots_results_text(merge_board_lots(boss_auction.boards_for_source(mid)))
               if mid else board_results_text(board))
    if not mid or not results:
        return
    info = load_active_messages().get(str(mid))
    if info is not None:
        boss_name, boss_time, loot = info.get("boss_name", ""), info.get("boss_time", ""), info.get("loot", "")
        with active_messages_lock:
            data = load_active_messages()
            if str(mid) in data:
                data[str(mid)]["loot_acquirers"] = results
                write_active_messages(data)
    else:
        bundle = await asyncio.to_thread(load_expired_message_bundle, int(mid))
        boss_name, boss_time, loot = bundle["boss_name"], bundle["boss_time"], bundle["loot"]
    await save_boss_loot_to_excel(int(mid), boss_name, boss_time, loot, "경매", acquirers=results)
    # 경매 결과는 알림에 싣지 않는다 — 이전 버전이 남긴 결과 줄이 있으면 지움 (버튼·이미지는 그대로)
    try:
        ch = bot.get_channel(ALERT_CHANNEL_ID) or await bot.fetch_channel(ALERT_CHANNEL_ID)
        msg = await ch.fetch_message(int(mid))
        if msg.embeds:
            embed = msg.embeds[0]
            desc = re.sub(r"\n(👤 \*\*획득자\*\*|🏆 \*\*경매 결과\*\*): [^\n]*", "", embed.description or "")
            if desc != (embed.description or ""):
                embed.description = desc
                await _edit_message_with_image(msg, embed=embed)
    except Exception as e:
        log.info(f"[진행판 결과 → 알림 갱신 실패] {mid}: {type(e).__name__}: {e}")
    log.info(f"[진행판 결과 반영] {boss_name} {boss_time} {mid} | {results[:120]}")


class AttendView(ui.View):
    def __init__(self, message_id=None, boss_name=None, boss_time=None, created_date=None, loot=None, expired=False, disable_time=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.channel_id = None  # loot 버튼 클릭 시 채워짐
        self.image_url = ""    # Discord CDN URL (edit 시 재사용)
        self.image_filename = ""  # attachment:// 재참조용 파일명
        self.loot_announcement_id = ""  # 득템 채널 공지 메시지 ID
        self.loot_acquirers = ""  # 획득자 (쉼표 구분 문자열)
        self.boss_name = boss_name or load_boss_name_from_excel(message_id) or ""
        self.attendees = load_attendees_from_excel(message_id) if message_id else []
        self.boss_time = boss_time or load_boss_time_from_excel(message_id) or datetime.now().strftime("%H:%M")
        self.created_date = created_date or datetime.now().strftime("%Y-%m-%d")
        if loot is None and message_id:
            loaded_loot, _, loaded_ann = load_loot_from_excel(message_id)
            self.loot = loaded_loot
            self.loot_announcement_id = loaded_ann or ""
            self.loot_acquirers = load_loot_acquirers_from_excel(message_id) or ""
        else:
            self.loot = loot or ""
        self.expired = expired
        self.disable_time = _coerce_disable_time(disable_time)
        self._view_lock = asyncio.Lock()
        # 파티 확정 상태
        self.party_confirmed = False
        self.confirmed_attendees: list = []
        self.confirmed_at: str = ""
        self.confirm_dt = None  # 자동 확정 예정 시각 (카운트다운용)
        if HIDE_ATTEND_BUTTONS:
            for child in list(self.children):
                if getattr(child, "custom_id", None) in ("boss_join", "boss_cancel"):
                    self.remove_item(child)
        if LOOT_AUCTION_BUTTON:
            for child in self.children:
                if getattr(child, "custom_id", None) == "boss_loot":
                    child.emoji = "📦"
        else:
            for child in list(self.children):
                if getattr(child, "custom_id", None) == "boss_auction_create":
                    self.remove_item(child)
        # 파티 확인 버튼 초기 비활성
        for child in self.children:
            if getattr(child, "custom_id", None) == "boss_party":
                child.disabled = True
        # 참석/취소 버튼은 마감 후 비활성
        if expired:
            for child in self.children:
                if getattr(child, "custom_id", None) in ATTEND_BUTTON_IDS:
                    child.disabled = True

    def _verify_attendees_from_log(self):
        """로그 리플레이 마감 검증 — 공용 엔진(RaidBook_attendance) 위임."""
        if not self.message_id:
            return self.attendees
        return rb_att.verify_attendees(
            self.message_id, LOG_DIR, "RaidBook_boss_Bot",
            self.attendees, _name_to_uid_mapping(),
            logger=log, label=f"{self.boss_name} {self.boss_time}")

    async def disable_buttons(self, message):
        # 다른 경로(음성 인원체크 등)에서 갱신된 참석자/득템 반영 후 로그 기반 검증
        if self.message_id:
            self._load_latest_from_json(self.message_id)
        verified_attendees = self._verify_attendees_from_log()
        self.attendees = verified_attendees
        self.expired = True

        # 참석/취소만 비활성. 획득 아이템 버튼은 마감 후에도 사용 가능하도록 유지
        for child in self.children:
            if getattr(child, "custom_id", None) in ATTEND_BUTTON_IDS:
                child.disabled = True

        id_to_name = _load_id_to_name_mapping()
        names = ", ".join(id_to_name.get(u, u) for u in self.attendees) if self.attendees else "없음"
        image_filename, image_url = _message_image_ref(message)
        if image_filename:
            self.image_filename = image_filename
        if image_url:
            self.image_url = image_url
        image_file, image_kwargs = _boss_image_file_and_kwargs(self.boss_name, message)
        embed = make_attend_embed(self.boss_name, len(self.attendees), names, self.boss_time,
                                  expired=True, loot=self.loot, disable_time=self.disable_time,
                                  loot_acquirers=getattr(self, "loot_acquirers", ""), **image_kwargs)
        await _edit_message_with_image(
            message,
            embed=embed,
            view=self,
            image_file=image_file,
            content=_boss_alert_content(self.boss_name, self.boss_time, expired=True),
        )

        # 마감 시 참석자 데이터를 Excel에 일괄 저장
        await save_boss_attendees_to_excel(self.message_id, self.boss_name, self.boss_time, self.attendees, self.created_date)

    def _restore_from_message(self, message_id):
        """봇 재시작 후 클릭 시 JSON → Excel 순으로 데이터 복구. 다른 메시지면 재복구."""
        if self.message_id == message_id:
            return
        self.message_id = message_id
        # JSON에서 먼저 복구 (마감 전 참석자 포함)
        active = load_active_messages()
        info = active.get(str(message_id))
        if info:
            self.attendees = list(info.get("attendees", []))
            self.boss_name = info.get("boss_name", "")
            self.boss_time = info.get("boss_time", "")
            self.created_date = info.get("created_date", datetime.now().strftime("%Y-%m-%d"))
            self.loot = info.get("loot", "") or ""
            self.loot_announcement_id = (
                info.get("loot_announcement_id", "")
                or info.get("announcement_msg_id", "")
                or self.loot_announcement_id
            )
            self.image_url = info.get("image_url", "") or self.image_url
            self.image_filename = info.get("image_filename", "") or self.image_filename
            self.disable_time = _coerce_disable_time(info.get("disable_time"))
            self.expired = False
            self.party_confirmed = info.get("party_confirmed", False)
            self.confirmed_attendees = list(info.get("confirmed_attendees", []))
            self.confirmed_at = info.get("confirmed_at", "")
            if self.party_confirmed:
                self._apply_confirmed_button_state()
        else:
            # JSON에 없으면 Excel에서 복구 (이미 마감된 메시지)
            self.attendees = load_attendees_from_excel(message_id)
            self.boss_name = load_boss_name_from_excel(message_id) or ""
            self.boss_time = load_boss_time_from_excel(message_id) or ""
            loot_text, _, ann_id = load_loot_from_excel(message_id)
            self.loot = loot_text or ""
            self.loot_announcement_id = ann_id or ""
            self.disable_time = None
            self.expired = True
            for child in self.children:
                if getattr(child, "custom_id", None) in ATTEND_BUTTON_IDS:
                    child.disabled = True

    def _load_latest_from_json(self, message_id):
        """매 클릭마다 JSON에서 최신 데이터 로드 (캐시 불신)"""
        self.message_id = message_id
        active = load_active_messages()
        info = active.get(str(message_id))
        if info:
            self.attendees = list(info.get("attendees", []))
            self.boss_name = info.get("boss_name", "") or self.boss_name
            self.boss_time = info.get("boss_time", "") or self.boss_time
            self.created_date = info.get("created_date", datetime.now().strftime("%Y-%m-%d"))
            self.loot = info.get("loot", "") or self.loot
            self.loot_acquirers = info.get("loot_acquirers", "") or self.loot_acquirers
            self.loot_announcement_id = (
                info.get("loot_announcement_id", "")
                or info.get("announcement_msg_id", "")
                or self.loot_announcement_id
            )
            self.image_url = info.get("image_url", "") or self.image_url
            self.image_filename = info.get("image_filename", "") or self.image_filename
            dt = _coerce_disable_time(info.get("disable_time"))
            if dt:
                self.disable_time = dt
            self.party_confirmed = info.get("party_confirmed", False)
            self.confirmed_attendees = list(info.get("confirmed_attendees", []))
            self.confirmed_at = info.get("confirmed_at", "")

    def _apply_confirmed_button_state(self):
        """파티 확정 후 버튼 상태 적용: 파티확인 활성"""
        for child in self.children:
            if getattr(child, "custom_id", None) == "boss_party":
                child.disabled = False

    def _get_countdown_text(self) -> str:
        """확정 전 남은 시간 텍스트 반환. confirm_dt 없으면 boss_time으로 재계산."""
        if self.party_confirmed:
            return ""
        confirm_dt = self.confirm_dt
        if confirm_dt is None and self.boss_time:
            boss_mins = _parse_boss_time_minutes(self.boss_time)
            if boss_mins is not None:
                confirm_minutes = _party_cfg.get("boss_confirm_minutes_before", 10)
                now = datetime.now()
                confirm_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=boss_mins - confirm_minutes)
                if confirm_dt <= now:
                    confirm_dt += timedelta(days=1)
        if confirm_dt is None:
            return ""
        remaining = (confirm_dt - datetime.now()).total_seconds()
        if remaining <= 0:
            return ""
        return f"⏳ 파티 확정까지 **{int(remaining // 60)}분**"

    def _make_confirmed_embed_kwargs(self, id_to_name):
        """파티 확정 시 embed kwargs 반환"""
        confirmed_names = ", ".join(id_to_name.get(u, u) for u in self.confirmed_attendees) if self.confirmed_attendees else "없음"
        late = [u for u in self.attendees if u not in self.confirmed_attendees]
        late_names = ", ".join(id_to_name.get(u, u) for u in late) if late else "없음"
        return {"confirmed_names": confirmed_names, "late_names": late_names if late else None, "confirmed_at": self.confirmed_at}

    async def do_party_confirm(self, channel_id: int):
        """파티 확정 실행 — 현재 attendees 고정, 메시지 편집"""
        if self.party_confirmed:
            return
        async with self._view_lock:
            if self.party_confirmed:
                return
            self._load_latest_from_json(self.message_id)
            self.confirmed_attendees = list(self.attendees)
            self.confirmed_at = datetime.now().strftime("%H:%M")
            self.party_confirmed = True
            # JSON 갱신
            with active_messages_lock:
                data = load_active_messages()
                key = str(self.message_id)
                if key in data:
                    data[key]["party_confirmed"] = True
                    data[key]["confirmed_attendees"] = self.confirmed_attendees
                    data[key]["confirmed_at"] = self.confirmed_at
                    write_active_messages(data)
            self._apply_confirmed_button_state()
            try:
                ch = bot.get_channel(channel_id)
                if ch is None:
                    ch = await bot.fetch_channel(channel_id)
                msg = await ch.fetch_message(self.message_id)
                id_to_name = _load_id_to_name_mapping()
                image_file, image_kwargs = _boss_image_file_and_kwargs(self.boss_name, msg)
                embed = make_attend_embed(
                    self.boss_name, len(self.attendees),
                    ", ".join(id_to_name.get(u, u) for u in self.attendees) or "없음",
                    self.boss_time, loot=self.loot, disable_time=self.disable_time,
                    loot_acquirers=getattr(self, "loot_acquirers", ""),
                    **image_kwargs, **self._make_confirmed_embed_kwargs(id_to_name),
                )
                await _edit_message_with_image(msg, embed=embed, view=self, image_file=image_file,
                                               content=_boss_alert_content(self.boss_name, self.boss_time))
                log.info(f"[파티 확정] {self.boss_name} {self.boss_time} | {len(self.confirmed_attendees)}명 확정")
            except Exception as e:
                log.warning(f"[파티 확정 메시지 편집 실패] {e}")

    @ui.button(label="참석", style=discord.ButtonStyle.success, custom_id="boss_join")
    async def join(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.defer()
        except Exception as e:
            log.info(f"[참석-defer 실패] msg:{interaction.message.id} user:{interaction.user.id} | {type(e).__name__}: {e}")
            return
        async with self._view_lock:
            self._load_latest_from_json(interaction.message.id)
            uid = str(interaction.user.id)
            name = interaction.user.display_name
            update_user_mapping(uid, name)
            if uid not in self.attendees:
                self.attendees.append(uid)
                # JSON에 키가 없으면(auto_close 후 삭제된 경우) upsert로 재등록 — 없으면 재시작 시 소멸
                _upsert_active_message_attendees(
                    self.message_id, self.attendees,
                    boss_name=self.boss_name, boss_time=self.boss_time,
                    created_date=self.created_date, disable_time=self.disable_time,
                    channel_id=interaction.channel_id, loot=self.loot,
                    image_url=getattr(self, "image_url", ""),
                    image_filename=getattr(self, "image_filename", ""),
                    loot_announcement_id=getattr(self, "loot_announcement_id", ""))
                log.info(rb_att.attend_log_line("참석", f"{self.boss_name} {self.boss_time}",
                                                self.message_id, name, uid, len(self.attendees)))
                ack_msg = f"🎉 {name}님, 참석 처리됐어요!"
                # close_minutes 비활성 시 마감 시점에 저장하는 기존 로직이 호출되지 않으므로
                # 버튼 클릭마다 Excel에도 즉시 반영해 데이터 유실 방지 (0은 정각 마감 = 활성)
                _cm = config.get("close_minutes")
                try: _cm = int(_cm) if _cm is not None else None
                except Exception: _cm = None
                if _cm is None or _cm < 0:
                    try:
                        await save_boss_attendees_to_excel(
                            self.message_id, self.boss_name, self.boss_time,
                            self.attendees, self.created_date)
                    except Exception as e:
                        log.info(f"[참석-즉시저장 실패] {e}")
            else:
                ack_msg = f"😊 {name}님은 이미 참석하고 있어요."
            id_to_name = _load_id_to_name_mapping()
            names = ", ".join(id_to_name.get(u, u) for u in self.attendees) if self.attendees else "없음"
            image_filename, image_url = _message_image_ref(interaction.message)
            if image_filename:
                self.image_filename = image_filename
            if image_url:
                self.image_url = image_url
            image_file, image_kwargs = _boss_image_file_and_kwargs(self.boss_name, interaction.message)
            confirmed_kwargs = self._make_confirmed_embed_kwargs(id_to_name) if self.party_confirmed else {}
            if self.party_confirmed:
                self._apply_confirmed_button_state()
            countdown_text = self._get_countdown_text()
            embed = make_attend_embed(self.boss_name, len(self.attendees), names, self.boss_time,
                                      expired=getattr(self, "expired", False), loot=self.loot,
                                      disable_time=self.disable_time,
                                      countdown_text=countdown_text,
                                      loot_acquirers=getattr(self, "loot_acquirers", ""),
                                      **image_kwargs, **confirmed_kwargs)
            try:
                await _edit_message_with_image(
                    interaction.message,
                    embed=embed,
                    view=self,
                    image_file=image_file,
                    content=_boss_alert_content(self.boss_name, self.boss_time, expired=getattr(self, "expired", False)),
                )
            except Exception:
                pass
            try:
                await interaction.followup.send(ack_msg, ephemeral=True)
            except Exception:
                pass

    @ui.button(label="취소", style=discord.ButtonStyle.secondary, custom_id="boss_cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.defer()
        except Exception:
            return
        async with self._view_lock:
            self._load_latest_from_json(interaction.message.id)
            uid = str(interaction.user.id)
            name = interaction.user.display_name
            update_user_mapping(uid, name)
            if uid in self.attendees:
                self.attendees.remove(uid)
                _upsert_active_message_attendees(
                    self.message_id, self.attendees,
                    boss_name=self.boss_name, boss_time=self.boss_time,
                    created_date=self.created_date, disable_time=self.disable_time,
                    channel_id=interaction.channel_id, loot=self.loot,
                    image_url=getattr(self, "image_url", ""),
                    image_filename=getattr(self, "image_filename", ""),
                    loot_announcement_id=getattr(self, "loot_announcement_id", ""))
                log.info(rb_att.attend_log_line("취소", f"{self.boss_name} {self.boss_time}",
                                                self.message_id, name, uid, len(self.attendees)))
                ack_msg = f"👋 {name}님, 참석을 취소했어요."
                # close_minutes 비활성 시 즉시 Excel 동기화 (유실 방지, 0은 정각 마감 = 활성)
                _cm = config.get("close_minutes")
                try: _cm = int(_cm) if _cm is not None else None
                except Exception: _cm = None
                if _cm is None or _cm < 0:
                    try:
                        await save_boss_attendees_to_excel(
                            self.message_id, self.boss_name, self.boss_time,
                            self.attendees, self.created_date)
                    except Exception as e:
                        log.info(f"[취소-즉시저장 실패] {e}")
            else:
                ack_msg = f"🤔 {name}님은 아직 참석하지 않으셨어요."
            id_to_name = _load_id_to_name_mapping()
            names = ", ".join(id_to_name.get(u, u) for u in self.attendees) if self.attendees else "없음"
            image_filename, image_url = _message_image_ref(interaction.message)
            if image_filename:
                self.image_filename = image_filename
            if image_url:
                self.image_url = image_url
            image_file, image_kwargs = _boss_image_file_and_kwargs(self.boss_name, interaction.message)
            confirmed_kwargs = self._make_confirmed_embed_kwargs(id_to_name) if self.party_confirmed else {}
            if self.party_confirmed:
                self._apply_confirmed_button_state()
            countdown_text = self._get_countdown_text()
            embed = make_attend_embed(self.boss_name, len(self.attendees), names, self.boss_time,
                                      expired=getattr(self, "expired", False), loot=self.loot,
                                      disable_time=self.disable_time,
                                      countdown_text=countdown_text,
                                      loot_acquirers=getattr(self, "loot_acquirers", ""),
                                      **image_kwargs, **confirmed_kwargs)
            try:
                await _edit_message_with_image(
                    interaction.message,
                    embed=embed,
                    view=self,
                    image_file=image_file,
                    content=_boss_alert_content(self.boss_name, self.boss_time, expired=getattr(self, "expired", False)),
                )
            except Exception:
                pass
            try:
                await interaction.followup.send(ack_msg, ephemeral=True)
            except Exception:
                pass

    @ui.button(label="인원체크", style=discord.ButtonStyle.success, custom_id="boss_voicecheck")
    async def voice_check_button(self, interaction: discord.Interaction, button: ui.Button):
        """voice_channel_ids(비면 전체 음성채널) 접속자를 이 알림의 참석자로 추가. 명령 권한 보유자만."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
        if not _is_bot_admin(interaction.user):
            try:
                await interaction.followup.send("⛔ 인원체크 권한이 없어요.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            result = await voice_check_message(interaction.message.id)
        except Exception as e:
            log.info(f"[인원체크 버튼 실패] msg:{interaction.message.id} | {type(e).__name__}: {e}")
            result = None
            text = "⚠️ 인원체크 중 오류가 발생했어요."
        else:
            if result is None:
                text = "⏰ 이미 마감된 알림이에요."
            else:
                added, total = result
                text = f"🎙️ 음성채널 인원체크 완료 — 신규 {added}명, 총 {total}명"
        log.info(f"[인원체크 버튼] msg:{interaction.message.id} user:{interaction.user.id} | {result}")
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass

    def _is_loot_attendee(self, user):
        uid = str(user.id)
        canonical_uid = _resolve_uid(uid)
        if uid in self.attendees or canonical_uid in {_resolve_uid(a) for a in self.attendees}:
            return True
        return uid in LOOT_FORCE_USER_IDS or canonical_uid in LOOT_FORCE_USER_IDS

    async def _load_for_modal(self, interaction: discord.Interaction):
        """팝업용 알림 데이터 로드. 진행 중 알림은 즉시(팝업을 첫 응답으로 띄울 수 있음),
        마감된 알림은 defer 후 Excel 조회. 반환: (deferred, 권한 여부)."""
        msg_id = interaction.message.id
        info = load_active_messages().get(str(msg_id))
        deferred = False
        if info is not None:
            self.attendees = list(info.get("attendees", []))
            self.boss_name = info.get("boss_name", "") or self.boss_name
            self.boss_time = info.get("boss_time", "") or self.boss_time
            self.loot = info.get("loot", "") or ""
            self.loot_acquirers = info.get("loot_acquirers", "") or ""
            self.loot_announcement_id = info.get("loot_announcement_id", "") or self.loot_announcement_id
            self.expired = False
        else:
            await interaction.response.defer(ephemeral=True)
            deferred = True
            bundle = await asyncio.to_thread(load_expired_message_bundle, msg_id)
            self.attendees = bundle["attendees"]
            self.boss_name = bundle["boss_name"] or self.boss_name
            self.boss_time = bundle["boss_time"] or self.boss_time
            self.loot = bundle["loot"]
            self.loot_acquirers = bundle["loot_acquirers"]
            self.loot_announcement_id = bundle["loot_announcement_id"]
            self.expired = True
        return deferred, (self._is_loot_attendee(interaction.user) or _is_bot_admin(interaction.user))

    async def _reply(self, interaction, deferred, text, view=None):
        kwargs = {"ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        if deferred:
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)

    async def _open_with_modal(self, interaction, deferred, make_modal, label, emoji):
        if deferred:
            await interaction.followup.send("마감된 알림이에요. 아래 버튼으로 입력창을 열어 주세요.",
                                            view=ModalOpenView(make_modal, label, emoji), ephemeral=True)
        else:
            await interaction.response.send_modal(make_modal())

    async def _open_loot_record(self, interaction: discord.Interaction):
        """📦 획득 아이템 — 기록만 하는 입력창."""
        deferred, allowed = await self._load_for_modal(interaction)
        if not allowed:
            await self._reply(interaction, deferred, "🤔 이 보스에 참석한 사람만 득템을 기록할 수 있어요.")
            return
        rev, prefill = get_loot_revision(interaction.message.id), self.loot or ""
        await self._open_with_modal(interaction, deferred, lambda: LootRecordModal(self, rev, prefill), "입력창 열기", "📦")

    @ui.button(label="획득 아이템", style=discord.ButtonStyle.primary, custom_id="boss_loot")
    async def loot_button(self, interaction: discord.Interaction, button: ui.Button):
        # persistent view는 모든 메시지가 같은 인스턴스를 공유하므로,
        # 매 클릭마다 해당 메시지의 데이터로 명시적으로 재로드 (이전 메시지 캐시 차단)
        msg_id = interaction.message.id
        if is_loot_processing(msg_id):
            await interaction.response.send_message(loot_busy_message(), ephemeral=True)
            return
        self.message_id = msg_id
        self.channel_id = interaction.channel_id  # on_submit에서 원본 메시지 fetch에 사용
        if LOOT_AUCTION_BUTTON:
            await self._open_loot_record(interaction)
            return
        # 마감 메시지의 Excel 재로드가 3초를 넘길 수 있어 응답을 먼저 예약
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
        active = load_active_messages()
        info = active.get(str(msg_id))
        if info:
            self.attendees = list(info.get("attendees", []))
            self.boss_name = info.get("boss_name", "") or self.boss_name
            self.boss_time = info.get("boss_time", "") or self.boss_time
            self.loot = info.get("loot", "") or ""
            self.loot_announcement_id = (
                info.get("loot_announcement_id", "")
                or info.get("announcement_msg_id", "")
                or self.loot_announcement_id
            )
        else:
            # 마감된 메시지 → Excel 에서 강제 재로드 (이전 캐시 무시)
            # 워크북 1회 로드로 일괄 조회 — 개별 4회 로드는 4초를 넘겨
            # Discord 3초 인터랙션 응답 시한을 초과했다.
            bundle = load_expired_message_bundle(msg_id)
            self.attendees = bundle["attendees"]
            if bundle["boss_name"]:
                self.boss_name = bundle["boss_name"]
            if bundle["boss_time"]:
                self.boss_time = bundle["boss_time"]
            self.loot = bundle["loot"]
            self.loot_acquirers = bundle["loot_acquirers"]
            self.loot_announcement_id = bundle["loot_announcement_id"]
            self.expired = True

        uid = str(interaction.user.id)
        name = interaction.user.display_name
        # alias 적용한 정규 UID 기준으로도 비교 (부계정/동명이인 통합 검사)
        canonical_uid = _resolve_uid(uid)
        attendee_canonicals = {_resolve_uid(a) for a in self.attendees}
        is_attendee = (uid in self.attendees) or (canonical_uid in attendee_canonicals)
        is_force_allowed = (uid in LOOT_FORCE_USER_IDS) or (canonical_uid in LOOT_FORCE_USER_IDS)

        if not is_attendee and not is_force_allowed:
            try:
                await interaction.followup.send(
                    f"🤔 {name}님은 이 보스에 참석하지 않으셔서 아이템 기록은 참석자만 가능해요.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        if is_force_allowed and not is_attendee:
            log.info(f"[loot 강제허용] {name}({uid}) → 보스 {self.boss_name} {self.boss_time} {self.message_id}")

        # 개수 선택 → 개별 입력 모달
        try:
            opened_revision = get_loot_revision(msg_id)
            await interaction.followup.send(
                "🎁 **획득한 아이템 개수를 선택해주세요.**",
                view=LootCountSelectView(
                    self,
                    existing_loot=self.loot or "",
                    opened_revision=opened_revision,
                    existing_acquirers=getattr(self, "loot_acquirers", "") or "",
                ),
                ephemeral=True)
            log.info(f"[loot 개수선택 전송] {self.boss_name} {self.boss_time} {msg_id}")
        except Exception as e:
            log.info(f"[loot 개수선택 전송 실패] {self.boss_name} {self.boss_time} {msg_id} | {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    f"⚠️ 획득 아이템 창을 여는 중 오류가 발생했어요: {type(e).__name__}",
                    ephemeral=True)
            except Exception:
                pass

    @ui.button(label="경매생성", emoji="🔨", style=discord.ButtonStyle.primary, custom_id="boss_auction_create")
    async def auction_create_button(self, interaction: discord.Interaction, button: ui.Button):
        """🔨 경매생성 — 기록된 득템 중 하나를 골라 시작가·호가·카운트를 넣어 경매장에 경매 메시지."""
        msg_id = interaction.message.id
        self.message_id = msg_id
        self.channel_id = interaction.channel_id
        deferred, allowed = await self._load_for_modal(interaction)
        if not allowed:
            await self._reply(interaction, deferred, "🤔 이 보스에 참석한 사람만 경매를 만들 수 있어요.")
            return
        options = auction_options(self.loot or "", boss_auction.boards_for_source(msg_id))
        if not options:
            text = ("📦 먼저 **획득 아이템** 버튼으로 아이템을 기록해 주세요." if not (self.loot or "").strip()
                    else "✅ 기록된 아이템이 모두 낙찰됐거나 경매 중이에요.")
            await self._reply(interaction, deferred, text)
            return
        async def _recheck():
            # 팝업을 연 사이 다른 사람이 같은 아이템 경매를 만들었는지 다시 확인
            loot = (load_active_messages().get(str(msg_id)) or {}).get("loot")
            if loot is None:
                loot, _, _ = await asyncio.to_thread(load_loot_from_excel, msg_id)
            return {o["label"] for o in auction_options(loot or "", boss_auction.boards_for_source(msg_id))}

        ctx = dict(title="🔨 경매 생성", options=options, defaults=AUCTION_DEFAULTS, boss_name=self.boss_name,
                   boss_time=self.boss_time, source_message_id=msg_id, recheck=_recheck)
        await self._open_with_modal(interaction, deferred, lambda: LotCreateModal(boss_auction, **ctx), "경매 생성", "🔨")

    @ui.button(label="파티 확인", style=discord.ButtonStyle.secondary, custom_id="boss_party", disabled=True)
    async def party_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        msg_id = interaction.message.id
        self._load_latest_from_json(msg_id)
        attendees = self.confirmed_attendees if self.party_confirmed else list(self.attendees)
        boss_name = self.boss_name

        if not attendees:
            # 활성 메시지 JSON에서 이미 마감/제거된 경우 Excel 데이터로 재조회
            attendees = load_attendees_from_excel(msg_id)
            if not boss_name:
                boss_name = load_boss_name_from_excel(msg_id) or "알 수 없음"

        if not attendees:
            try:
                await interaction.followup.send("참석자가 없어요.", ephemeral=True)
            except Exception:
                pass
            return

        parties, warnings = compose_parties(attendees, BASE_DIR)
        fields = format_party_embed_fields(parties, warnings)

        label = "확정 " if self.party_confirmed else ""
        embed = discord.Embed(
            title=f"⚔️ {boss_name} {label}파티 구성 ({len(attendees)}명 → {len(parties)}파티)",
            color=0x5865F2,
        )
        for f in fields:
            embed.add_field(name=f["name"], value=f["value"], inline=f["inline"])

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            pass


def _get_boss_image_path(boss_name: str) -> str | None:
    """boss_config에서 보스 이미지 경로를 반환. 없거나 파일 없으면 None."""
    all_boss_cfg = {**BOSSES_CONFIG, **DAILY_BOSSES_CONFIG}
    cfg = all_boss_cfg.get(boss_name, {})
    image_val = cfg.get("image", "").strip()
    if not image_val:
        return None
    # 상대 경로면 스크립트 기준으로 변환
    path = image_val if os.path.isabs(image_val) else os.path.join(_SCRIPT_DIR, image_val)
    return path if os.path.exists(path) else None


def _make_boss_file(boss_name: str):
    """보스 이미지 discord.File 반환. 전송 전 고정 캔버스로 정규화한다."""
    path = _get_boss_image_path(boss_name)
    if not path:
        return None
    try:
        canvas_size = parse_canvas_size(config.get("image_canvas_size") or config.get("image_size"))
        file_obj, filename = normalized_image_file_args(
            path,
            filename_prefix="boss",
            canvas_size=canvas_size,
            logger=log,
        )
        if not file_obj:
            return None
        return discord.File(file_obj, filename=filename)
    except Exception as e:
        log.info(f"[보스 이미지 로드 실패] {boss_name}: {e}")
        return None


def _message_image_ref(message):
    """Return (attachment_filename, cdn_url) for an image already attached to a Discord message."""
    for attachment in getattr(message, "attachments", []) or []:
        filename = getattr(attachment, "filename", "") or ""
        url = getattr(attachment, "url", "") or ""
        content_type = getattr(attachment, "content_type", "") or ""
        ext = os.path.splitext(filename)[1].lower()
        if content_type.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            return filename, url
    return "", ""


def _boss_image_file_and_kwargs(boss_name, message=None):
    # 이미 이미지가 첨부된 메시지는 기존 첨부를 재사용 (편집마다 이미지 재가공·재업로드 → 반응 지연 방지)
    image_filename, image_url = _message_image_ref(message) if message else ("", "")
    if image_filename:
        return None, {"image_url": image_url, "image_filename": image_filename}
    image_file = _make_boss_file(boss_name)
    if image_file:
        return image_file, {"thumbnail_file": image_file}
    return None, {"image_url": image_url, "image_filename": image_filename}


async def _edit_message_with_image(message, *, embed, view=discord.utils.MISSING,
                                   image_file=None, content=discord.utils.MISSING):
    kwargs = {"embed": embed}
    if view is not discord.utils.MISSING:
        kwargs["view"] = view
    if content is not discord.utils.MISSING:
        kwargs["content"] = content
    if image_file:
        kwargs["attachments"] = [image_file]
    elif getattr(message, "attachments", None):
        # 기존 첨부 이미지를 명시적으로 유지 (attachment:// 참조 보장, 재업로드 없음)
        kwargs["attachments"] = list(message.attachments)
    try:
        return await message.edit(**kwargs)
    except TypeError:
        if not image_file:
            raise
        kwargs.pop("attachments", None)
        kwargs["file"] = image_file
        return await message.edit(**kwargs)


def _embed_image_mode():
    mode = str(config.get("image_display", config.get("image_mode", "banner")) or "banner").strip().lower()
    if mode in ("thumbnail", "thumb", "small"):
        return "thumbnail"
    if mode in ("both", "all"):
        return "both"
    return "banner"


def _apply_embed_image(embed, *, thumbnail_file=None, image_url="", image_filename=""):
    image_ref = ""
    if thumbnail_file:
        image_ref = f"attachment://{thumbnail_file.filename}"
    elif image_filename:
        image_ref = f"attachment://{image_filename}"
    elif image_url:
        image_ref = image_url
    if not image_ref:
        return

    mode = _embed_image_mode()
    if mode == "thumbnail":
        embed.set_thumbnail(url=image_ref)
    elif mode == "both":
        embed.set_thumbnail(url=image_ref)
        embed.set_image(url=image_ref)
    else:
        embed.set_image(url=image_ref)


def make_attend_embed(boss_name, count, names, boss_time=None, expired=False, loot="",
                      disable_time=None, thumbnail_file=None, image_url="", image_filename="",
                      confirmed_names=None, late_names=None, confirmed_at="", countdown_text="",
                      loot_acquirers=""):
    embed = discord.Embed(color=0x888888 if expired else 0x00ff00)
    embed.title = f"📢 {boss_name} 보스 출현 알림"
    if expired:
        status = "🔒 **마감됨**"
    else:
        dt = _coerce_disable_time(disable_time)
        if dt:
            unix_ts = int(dt.timestamp())
            status = f"🔒 마감 <t:{unix_ts}:R> (<t:{unix_ts}:t>)\n보스 탐에 참여하실 분은 아래 버튼을 눌러주세요!"
        else:
            status = "보스 탐에 참여하실 분은 아래 버튼을 눌러주세요!"

    if confirmed_names is not None:
        # 파티 확정 후: 확정/미확정 분리 표시
        confirmed_count = len(confirmed_names.split(", ")) if confirmed_names and confirmed_names != "없음" else 0
        late_count = len(late_names.split(", ")) if late_names and late_names != "없음" else 0
        attendee_block = (
            f"🎯 파티 편성 ({confirmed_count}명) | {confirmed_names}\n"
        )
        if late_names and late_names != "없음":
            attendee_block += f"⚠️ 파티 미편성 ({late_count}명) | {late_names}"
        if confirmed_at:
            attendee_block += f"\n🔐 파티 확정: **{confirmed_at}**"
    else:
        attendee_block = f"✅ {'최종' if expired else '현재'} 참석: {count}명 | {names}"

    desc = (
        f"⏰ 보스 리젠 시각: **{boss_time}**\n"
        f"{status}\n\n"
        f"{attendee_block}"
    )
    if countdown_text:
        desc += f"\n\n{countdown_text}"
    if loot:
        desc += f"\n\n🎁 **획득 아이템**: {loot}"
        # 경매 모드에서는 낙찰자·낙찰가를 공개 알림에 싣지 않는다 (경매장 메시지와 Excel에만)
        if loot_acquirers and not LOOT_AUCTION_BUTTON:
            desc += f"\n👤 **획득자**: {loot_acquirers}"
    embed.description = desc
    _apply_embed_image(embed, thumbnail_file=thumbnail_file, image_url=image_url, image_filename=image_filename)
    return embed


def _parse_boss_time_minutes(value):
    text = str(value or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour == 24 and minute == 0:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _format_boss_time_minutes(total_minutes):
    total_minutes = int(total_minutes) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _next_boss_time(boss_name, boss_time):
    current = _parse_boss_time_minutes(boss_time)
    if current is None:
        return ""

    daily_cfg = DAILY_BOSSES_CONFIG.get(boss_name) or {}
    daily_times = []
    for raw_time in daily_cfg.get("times", []) or []:
        minutes = _parse_boss_time_minutes(raw_time)
        if minutes is not None:
            daily_times.append(minutes)
    if daily_times:
        daily_times = sorted(set(daily_times))
        for minutes in daily_times:
            if minutes > current:
                return _format_boss_time_minutes(minutes)
        return _format_boss_time_minutes(daily_times[0])

    interval = None
    if boss_name in boss_states:
        interval = boss_states[boss_name].get("interval_hours")
    if interval is None:
        interval = (BOSSES_CONFIG.get(boss_name) or {}).get("interval_hours")
    try:
        interval_minutes = int(float(interval) * 60)
    except Exception:
        interval_minutes = 0
    if interval_minutes <= 0:
        return ""
    return _format_boss_time_minutes(current + interval_minutes)


def _boss_alert_content(boss_name, boss_time=None, *, expired=False):
    if expired:
        next_time = _next_boss_time(boss_name, boss_time)
        return f"📢 {boss_name} 보스 마감" + (f" | 다음 리젠 {next_time}" if next_time else "")
    status = "알림"
    time_part = f" | 리젠 {boss_time}" if boss_time else ""
    return f"📢 {boss_name} 보스 {status}{time_part}"


def _schedule_party_confirm_task(attend_view: "AttendView", message_id: int, channel_id: int, boss_time_str: str):
    """보스 리젠 시각 기준 N분 전에 파티 자동 확정 + 1분마다 카운트다운 embed 업데이트."""
    confirm_minutes = _party_cfg.get("boss_confirm_minutes_before", 10)
    boss_mins = _parse_boss_time_minutes(boss_time_str)
    if boss_mins is None:
        return
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    boss_dt = today_midnight + timedelta(minutes=boss_mins)
    confirm_dt = today_midnight + timedelta(minutes=boss_mins - confirm_minutes)
    if confirm_dt <= now:
        if now < boss_dt:
            # 확정 시각은 지났지만 스폰 전 → 즉시 확정
            confirm_dt = now
        else:
            # 스폰도 지남 → 내일 확정 시각으로
            confirm_dt += timedelta(days=1)
    attend_view.confirm_dt = confirm_dt

    async def _countdown_loop():
        while not attend_view.party_confirmed:
            remaining = (confirm_dt - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            try:
                mins = int(remaining // 60)
                ch = bot.get_channel(channel_id)
                if ch is None:
                    ch = await bot.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                # 인원체크 등 다른 경로에서 갱신된 참석자 반영 (메모리 캐시로 덮어쓰지 않도록)
                attend_view._load_latest_from_json(message_id)
                if attend_view.party_confirmed:
                    break
                id_to_name = _load_id_to_name_mapping()
                names = ", ".join(id_to_name.get(u, u) for u in attend_view.attendees) if attend_view.attendees else "없음"
                image_file, image_kwargs = _boss_image_file_and_kwargs(attend_view.boss_name, msg)
                embed = make_attend_embed(
                    attend_view.boss_name, len(attend_view.attendees), names,
                    attend_view.boss_time, loot=attend_view.loot, disable_time=attend_view.disable_time,
                    loot_acquirers=getattr(attend_view, "loot_acquirers", ""),
                    **image_kwargs,
                    countdown_text=f"⏳ 파티 확정까지 **{mins}분**",
                )
                await _edit_message_with_image(msg, embed=embed, view=attend_view, image_file=image_file,
                                               content=_boss_alert_content(attend_view.boss_name, attend_view.boss_time))
            except Exception as e:
                log.debug(f"[카운트다운 업데이트 실패] {e}")
            await asyncio.sleep(60)

    async def _auto_confirm():
        delay = (confirm_dt - datetime.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        if not attend_view.party_confirmed:
            attend_view.message_id = message_id
            await attend_view.do_party_confirm(channel_id)
            log.info(f"[파티 자동 확정] 메시지 {message_id} | 리젠 {confirm_minutes}분 전")

    asyncio.ensure_future(_countdown_loop())
    asyncio.ensure_future(_auto_confirm())


async def _post_boss_alert(boss_name, spawn_dt):
    """보스 알림 메시지 1건 게시 (interval/daily 공용 콜백).
    메시지 생성·활성 저장·TTS·자동 마감·파티 확정까지 담당. 타이밍은 RaidBook_schedule 엔진."""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ALERT_CHANNEL_ID)
        except Exception as e:
            log.warning(f"[알림 실패] {boss_name} | 채널 조회 실패: {e}")
            return
    if channel is None:
        log.warning(f"[알림 실패] {boss_name} | 알림 채널을 찾을 수 없음 (ALERT_CHANNEL_ID={ALERT_CHANNEL_ID})")
        return

    boss_time_str = spawn_dt.strftime("%H:%M")
    # config의 close_minutes 기준 자동 마감. 0 → 등장 시각 정각 마감, null/없음/음수 → 자동 마감 비활성화 (무기한 유지)
    _cm = config.get("close_minutes")
    try:
        _cm = int(_cm) if _cm is not None else None
    except Exception:
        _cm = None
    disable_time = (spawn_dt + timedelta(minutes=_cm)) if _cm is not None and _cm >= 0 else None

    attend_view = AttendView(boss_name=boss_name, boss_time=boss_time_str, disable_time=disable_time)
    _bf = _make_boss_file(boss_name)
    _embed = make_attend_embed(boss_name, 0, "없음", boss_time_str,
                               disable_time=disable_time, thumbnail_file=_bf)
    msg = await channel.send(content=_boss_alert_content(boss_name, boss_time_str),
                             embed=_embed, view=attend_view,
                             file=_bf if _bf else discord.utils.MISSING)
    attend_view.message_id = msg.id
    # 이미지 CDN URL 저장 (이후 edit 시 재사용)
    if msg.attachments:
        attend_view.image_url = msg.attachments[0].url
        attend_view.image_filename = msg.attachments[0].filename
    log.info(f"[알림 전송] {boss_name} {boss_time_str} | 메시지 ID: {msg.id}"
             f" | 마감: {disable_time.strftime('%H:%M') if disable_time else '비활성화'}")

    # 시트 날짜는 '등장 시각(spawn_dt)' 기준. 알림은 20분 전(전날 23:40)에 뜨므로 now를 쓰면
    # 자정(00:00) 보스가 전날 시트에 저장돼 등장일로 조회 시 못 찾는 문제가 있었음.
    created_date = spawn_dt.strftime("%Y-%m-%d")
    attend_view.created_date = created_date
    disable_str = disable_time.strftime("%Y-%m-%d %H:%M:%S") if disable_time else ""
    save_active_message(msg.id, channel.id, boss_name, boss_time_str, disable_str,
                        created_date=created_date, image_url=attend_view.image_url,
                        image_filename=attend_view.image_filename)

    _schedule_party_confirm_task(attend_view, msg.id, channel.id, boss_time_str)
    _schedule_voice_check(msg.id, spawn_dt)

    if disable_time is not None:
        async def auto_disable():
            delay = (disable_time - datetime.now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await attend_view.disable_buttons(msg)
                log.info(f"[자동 마감] {boss_name} {boss_time_str} | 참석: {len(attend_view.attendees)}명")
            except Exception as e:
                log.info(f"[자동 마감 실패] {boss_name} {boss_time_str} {msg.id}: {type(e).__name__}: {e} — 점검 루프가 재시도")
                return
            remove_active_message(msg.id)

        asyncio.ensure_future(auto_disable())


def _collect_voice_members(guild, voice_channel=None) -> dict:
    """음성채널 접속자 {uid: 표시이름} (봇 제외).
    voice_channel 지정 시 그 채널만, 아니면 voice_channel_ids → 비면 전체 음성채널(AFK 제외)."""
    if guild is None:
        return {}
    if voice_channel is not None:
        channels = [voice_channel]
    elif VOICE_CHANNEL_IDS:
        channels = [guild.get_channel(cid) for cid in VOICE_CHANNEL_IDS]
    else:
        afk_id = guild.afk_channel.id if guild.afk_channel else None
        channels = [c for c in list(guild.voice_channels) + list(guild.stage_channels) if c.id != afk_id]
    members = {}
    for ch in channels:
        if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            continue
        for m in ch.members:
            if not m.bot:
                members[str(m.id)] = m.display_name
    return members


async def voice_check_message(message_id, voice_channel=None) -> tuple | None:
    """활성 보스 알림 1건에 음성채널 접속자를 참석자로 추가하고 메시지 갱신.
    [참석] 로그를 남겨 마감 시 로그 검증에서도 유지된다. 반환: (추가 수, 총원) / 활성 아님 None."""
    key = str(message_id)
    info = load_active_messages().get(key)
    if not info:
        return None
    ch = bot.get_channel(info["channel_id"])
    if ch is None:
        try:
            ch = await bot.fetch_channel(info["channel_id"])
        except Exception as e:
            log.info(f"[인원체크 실패] {key}: 채널 조회 실패 {e}")
            return None
    members = _collect_voice_members(ch.guild, voice_channel)

    added = []
    with active_messages_lock:
        data = load_active_messages()
        info = data.get(key)
        if not info:
            return None
        attendees = list(info.get("attendees", []))
        label = f"{info.get('boss_name', '')} {info.get('boss_time', '')}"
        for uid, name in members.items():
            if uid in attendees:
                continue
            attendees.append(uid)
            added.append((uid, name))
            log.info(rb_att.attend_log_line("참석", label, key, name, uid, len(attendees)))
        if added:
            info["attendees"] = attendees
            write_active_messages(data)
    log.info(f"[인원체크] {label} | 음성 {len(members)}명, 신규 {len(added)}명, 총 {len(attendees)}명")

    for uid, name in added:
        update_user_mapping(uid, name)

    # 신규가 없어도 메시지는 최신 참석자로 다시 그림 (표시가 어긋난 경우 복구)
    # close_minutes 비활성 시 마감 저장이 없으므로 참석 버튼과 동일하게 Excel 즉시 반영
    _cm = config.get("close_minutes")
    try: _cm = int(_cm) if _cm is not None else None
    except Exception: _cm = None
    if added and (_cm is None or _cm < 0):
        try:
            await save_boss_attendees_to_excel(int(key), info.get("boss_name", ""), info.get("boss_time", ""),
                                               attendees, info.get("created_date"))
        except Exception as e:
            log.info(f"[인원체크-즉시저장 실패] {e}")

    try:
        msg = await ch.fetch_message(int(key))
        # message_id 없이 생성 → 생성자의 Excel 조회(1MB 통합문서 여러 번 로드) 생략, JSON에서만 복원
        view = AttendView(boss_name=info.get("boss_name", ""), boss_time=info.get("boss_time", ""),
                          created_date=info.get("created_date"), loot=info.get("loot", "") or "",
                          disable_time=info.get("disable_time"))
        view._load_latest_from_json(int(key))
        if view.party_confirmed:
            view._apply_confirmed_button_state()
        id_to_name = _load_id_to_name_mapping()
        names = ", ".join(id_to_name.get(u, u) for u in view.attendees) if view.attendees else "없음"
        image_file, image_kwargs = _boss_image_file_and_kwargs(view.boss_name, msg)
        confirmed_kwargs = view._make_confirmed_embed_kwargs(id_to_name) if view.party_confirmed else {}
        embed = make_attend_embed(view.boss_name, len(view.attendees), names, view.boss_time,
                                  loot=view.loot, disable_time=view.disable_time,
                                  countdown_text=view._get_countdown_text(),
                                  loot_acquirers=getattr(view, "loot_acquirers", ""),
                                  **image_kwargs, **confirmed_kwargs)
        await _edit_message_with_image(msg, embed=embed, view=view, image_file=image_file,
                                       content=_boss_alert_content(view.boss_name, view.boss_time))
    except Exception as e:
        log.info(f"[인원체크-메시지 갱신 실패] {key}: {type(e).__name__}: {e}")
    return len(added), len(attendees)


def _schedule_voice_check(message_id, spawn_dt):
    """보스 출현 정각에 음성채널 인원체크 1회 예약."""
    if not VOICE_CHECK_AT_SPAWN or spawn_dt is None:
        return
    delay = (spawn_dt - datetime.now()).total_seconds()
    if delay < 0:
        return

    async def _run():
        await asyncio.sleep(delay)
        try:
            await voice_check_message(message_id)
        except Exception as e:
            log.info(f"[정각 인원체크 실패] {message_id}: {type(e).__name__}: {e}")

    asyncio.ensure_future(_run())


def _alert_close_time(info):
    """알림 마감 시각. 저장된 disable_time 이 없으면 (구버전/이전 데이터) 출현 시각 + close_minutes 로 계산."""
    dt = _coerce_disable_time(info.get("disable_time") or None)
    if dt:
        return dt
    try:
        cm = int(config.get("close_minutes")) if config.get("close_minutes") is not None else None
    except Exception:
        cm = None
    if cm is None or cm < 0:
        return None
    try:
        spawn = datetime.strptime(f"{info.get('created_date')} {info.get('boss_time')}", "%Y-%m-%d %H:%M")
    except Exception:
        return None
    return spawn + timedelta(minutes=cm)


_CLOSE_SWEEP_GRACE_SEC = 90   # 개별 마감 타이머가 먼저 처리하도록 여유


async def _sweep_overdue_alerts():
    """마감 시각이 지났는데 아직 활성인 알림을 마감 (타이머 유실·재시작·실패 대비)."""
    now = datetime.now()
    for mid, info in list(load_active_messages().items()):
        close_at = _alert_close_time(info)
        if close_at is None or (now - close_at).total_seconds() < _CLOSE_SWEEP_GRACE_SEC:
            continue
        try:
            ch = bot.get_channel(int(info["channel_id"])) or await bot.fetch_channel(int(info["channel_id"]))
            msg = await ch.fetch_message(int(mid))
        except discord.NotFound:
            remove_active_message(mid)
            log.info(f"[마감 점검] {mid} 메시지/채널 없음 → 활성 목록에서 제거")
            continue
        except Exception as e:
            log.info(f"[마감 점검] {mid} 조회 실패: {type(e).__name__}: {e}")
            continue
        view = AttendView(message_id=int(mid), boss_name=info.get("boss_name", ""),
                          boss_time=info.get("boss_time", ""), created_date=info.get("created_date"),
                          loot=info.get("loot", "") or "", disable_time=close_at)
        view.attendees = list(info.get("attendees", []))
        view.loot_acquirers = info.get("loot_acquirers", "") or ""
        try:
            await view.disable_buttons(msg)
        except Exception as e:
            log.info(f"[마감 점검] {mid} 마감 실패: {type(e).__name__}: {e}")
            continue
        remove_active_message(mid)
        log.info(f"[마감 점검] {info.get('boss_name')} {info.get('boss_time')} {mid} 마감 처리 "
                 f"(예정 {close_at.strftime('%m-%d %H:%M')})")


async def _close_sweep_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await _sweep_overdue_alerts()
        except Exception as e:
            log.info(f"[마감 점검 실패] {type(e).__name__}: {e}")


_close_sweep_task = None


async def schedule_boss(boss_name, alert_time, interval_hours):
    """interval형 보스 스케줄 — 타이밍은 공용 엔진(RaidBook_schedule), 게시는 _post_boss_alert."""
    await rb_schedule.run_interval(
        boss_name, alert_time, interval_hours, DEFAULT_ALERT_MINUTES,
        _post_boss_alert, boss_states, log)


async def shutdown():
    try:
        # 활성 메시지 마감 처리 + Excel 저장
        active_messages = load_active_messages()
        for msg_id_str, info in list(active_messages.items()):
            try:
                ch = bot.get_channel(info["channel_id"])
                if not ch:
                    continue
                msg = await ch.fetch_message(int(msg_id_str))
                attend_view = AttendView(
                    message_id=int(msg_id_str),
                    boss_name=info["boss_name"],
                    boss_time=info["boss_time"],
                    created_date=info.get("created_date"),
                    loot=info.get("loot", ""),
                )
                attend_view.attendees = list(info.get("attendees", []))
                attend_view.loot_announcement_id = (
                    info.get("loot_announcement_id", "")
                    or info.get("announcement_msg_id", "")
                    or ""
                )
                await attend_view.disable_buttons(msg)
                remove_active_message(msg_id_str)
                log.info(f"[종료 마감] {info['boss_name']} {info['boss_time']} | 참석: {len(attend_view.attendees)}명")
            except Exception as e:
                log.info(f"[종료 마감 실패] {msg_id_str}: {e}")

        channel = bot.get_channel(COMMAND_CHANNEL_ID)
        if channel:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            await channel.send(f"🔴 **보스 알림 봇이 종료되었습니다.** `{now}`")
    except Exception:
        pass
    await bot.close()


def signal_handler(sig, frame):
    asyncio.ensure_future(shutdown())


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@bot.event
async def on_message(message):
    # ! 접두사 명령은 사용하지 않음 — 모든 명령은 슬래시(/) 명령으로만 처리
    return


def _is_bot_admin(member) -> bool:
    """명령 사용 권한: 서버 소유자, 서버 관리/관리자 권한 또는 config admin_role_ids 역할 보유."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild is not None and member.id == member.guild.owner_id:
        return True
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    return any(r.id in ADMIN_ROLE_IDS for r in member.roles)


boss_auction = BossAuction(bot, channel_id=AUCTION_CHANNEL_ID, data_dir=DATA_DIR,
                           is_admin=_is_bot_admin, logger=log, currency=AUCTION_CURRENCY,
                           get_attendees=_boss_attendees_for)
boss_auction.on_board_finished = _apply_board_results
boss_auction.board_defaults = dict(AUCTION_DEFAULTS)


@bot.check
async def _slash_permission_check(ctx):
    """슬래시(/) 명령은 채널 제한 없이 사용자 권한만 확인."""
    if ctx.interaction is None:
        return False  # ! 접두사 명령 비활성 (on_message에서도 처리하지 않음)
    if _is_bot_admin(ctx.author):
        return True
    raise commands.CheckFailure("이 명령을 사용할 권한이 없어요. (서버 관리 권한 또는 관리자 역할 필요)")


@bot.before_invoke
async def before_any_command(ctx):
    if ctx.interaction is not None:
        args = " ".join(f"{k}={v}" for k, v in (ctx.interaction.namespace.__dict__ or {}).items())
        log.info(f"[명령어] /{ctx.command.name} | 사용자: {ctx.author.display_name} | 인자: {args}")
        # 슬래시 명령은 3초 안에 응답해야 하므로 먼저 '생각 중' 표시 → 이후 ctx.send는 followup
        if not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except Exception as e:
                log.info(f"[명령어] defer 실패: {e}")
    else:
        log.info(f"[명령어] !{ctx.command.name} | 사용자: {ctx.author.display_name} | 인자: {ctx.message.content}")


@bot.after_invoke
async def after_any_command(ctx):
    log.info(f"[완료] {'/' if ctx.interaction else '!'}{ctx.command.name}")


async def schedule_boss_fixed_time(boss_name, spawn_time_str, alert_minutes):
    """매일 고정 시간에 반복되는 보스 알림 (daily_bosses 방식).
    spawn_time_str: "HH:MM" 또는 "24:00"(→ 다음날 00:00)
    타이밍은 공용 엔진(RaidBook_schedule), 게시는 _post_boss_alert."""
    await rb_schedule.run_daily(
        boss_name, spawn_time_str, alert_minutes,
        _post_boss_alert, boss_daily_states, log)



@bot.hybrid_command()
async def startdailyboss(ctx, boss_name: str = None):
    """config daily_bosses의 보스를 매일 반복 알림으로 시작. 예: /startdailyboss 드레이크"""
    if not boss_name:
        names = list(DAILY_BOSSES_CONFIG.keys())
        await ctx.send(f"사용법: `/startdailyboss [보스이름]`\n등록된 보스: {', '.join(names) or '없음'}")
        return

    cfg = DAILY_BOSSES_CONFIG.get(boss_name)
    if not cfg:
        await ctx.send(f"❌ `{boss_name}`은 config의 daily_bosses에 없어요.")
        return

    times = cfg.get("times", [])
    if not times:
        await ctx.send(f"❌ `{boss_name}` times가 비어있어요.")
        return
    alert_min = cfg.get("alert_minutes", DEFAULT_ALERT_MINUTES)

    # 기존 태스크 취소
    if boss_name in boss_daily_states:
        for task in boss_daily_states[boss_name].values():
            if not task.done():
                task.cancel()

    boss_daily_states[boss_name] = {}
    for t in times:
        task = asyncio.ensure_future(schedule_boss_fixed_time(boss_name, t, alert_min))
        boss_daily_states[boss_name][t] = task
    _publish_boss_schedule(boss_name, {"type": "daily", "times": list(times)})

    await ctx.send(
        f"✅ **{boss_name}** 매일 반복 알림 시작!\n"
        f"시간: {', '.join(times)}\n"
        f"알림: 출현 **{alert_min}분** 전")


@bot.hybrid_command()
async def stopdailyboss(ctx, boss_name: str = None):
    """매일 반복 보스 알림 중지. 이름 없으면 전체 중지. 예: /stopdailyboss 드레이크"""
    if boss_name:
        if boss_name in boss_daily_states:
            for task in boss_daily_states.pop(boss_name).values():
                if not task.done():
                    task.cancel()
            _remove_boss_schedule(boss_name)
            await ctx.send(f"⛔ **{boss_name}** 매일 반복 알림이 중지됐습니다.")
        else:
            await ctx.send(f"❌ **{boss_name}** daily 스케줄이 없어요.")
    else:
        if not boss_daily_states:
            await ctx.send("❌ 실행 중인 daily 알림이 없어요.")
            return
        names = list(boss_daily_states.keys())
        for name in names:
            for task in boss_daily_states[name].values():
                if not task.done():
                    task.cancel()
            _remove_boss_schedule(name)
        boss_daily_states.clear()
        await ctx.send(f"⛔ 모든 daily 알림이 중지됐습니다. ({', '.join(names)})")


@bot.hybrid_command()
async def listdailyboss(ctx):
    """매일 반복 보스 알림 목록"""
    if not boss_daily_states:
        await ctx.send("❌ 실행 중인 daily 알림이 없어요.")
        return
    lines = ["📋 **매일 반복 보스 알림**"]
    for name, time_tasks in boss_daily_states.items():
        cfg = DAILY_BOSSES_CONFIG.get(name, {})
        alert_min = cfg.get("alert_minutes", DEFAULT_ALERT_MINUTES)
        times_str = ", ".join(time_tasks.keys())
        lines.append(f"  🟢 **{name}** — {times_str} | {alert_min}분 전 알림")
    await ctx.send("\n".join(lines))


@bot.hybrid_command()
async def startboss(ctx, boss_name: str, time_str: str, interval: float = None):
    """보스 스케줄 시작. 예: /startboss 카스파 13:00 또는 /startboss 카스파 13:00 3"""
    try:
        hour, minute = map(int, time_str.split(":"))
        now = datetime.now()

        # 주기: 인자 > config > 기본값 2시간
        if interval is None:
            boss_cfg = BOSSES_CONFIG.get(boss_name, {})
            interval = boss_cfg.get("interval_hours", 2)

        boss_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        alert_time = boss_time - timedelta(minutes=DEFAULT_ALERT_MINUTES)

        # 이미 지난 시간이면 주기 단위로 다음 알림 시간 계산
        while alert_time < now:
            boss_time += timedelta(hours=interval)
            alert_time = boss_time - timedelta(minutes=DEFAULT_ALERT_MINUTES)

        # 기존 스케줄 취소
        if boss_name in boss_states:
            old_task = boss_states[boss_name].get("task")
            if old_task and not old_task.done():
                old_task.cancel()

        task = asyncio.ensure_future(schedule_boss(boss_name, alert_time, interval))
        boss_states[boss_name] = {"task": task, "interval_hours": interval}
        _publish_boss_schedule(boss_name, {"type": "interval", "interval_hours": interval,
                                           "start_hour": hour, "start_minute": minute})

        await ctx.send(
            f"✅ **{boss_name}** 보스 출현 **{boss_time.strftime('%H:%M')}** 기준으로 시작!\n"
            f"첫 알림: **{alert_time.strftime('%H:%M')}**\n"
            f"주기: **{interval}시간**"
        )
    except Exception:
        await ctx.send("❌ 형식이 잘못됐어요! 예: `/startboss 카스파 13:00` 또는 `/startboss 카스파 13:00 3`")


@bot.hybrid_command()
async def stopboss(ctx, boss_name: str = None):
    """보스 스케줄 중지. 이름 없으면 전체 중지. 예: /stopboss 카스파"""
    if boss_name:
        if boss_name in boss_states:
            task = boss_states[boss_name].get("task")
            if task and not task.done():
                task.cancel()
            del boss_states[boss_name]
            _remove_boss_schedule(boss_name)
            await ctx.send(f"⛔ **{boss_name}** 보스 알림이 중지됐습니다.")
        else:
            await ctx.send(f"❌ **{boss_name}** 스케줄이 없어요.")
    else:
        if not boss_states:
            await ctx.send("❌ 실행 중인 알림이 없어요.")
            return
        names = list(boss_states.keys())
        for name in names:
            task = boss_states[name].get("task")
            if task and not task.done():
                task.cancel()
            _remove_boss_schedule(name)
        boss_states.clear()
        await ctx.send(f"⛔ 모든 보스 알림이 중지됐습니다. ({', '.join(names)})")


@bot.hybrid_command()
async def setboss(ctx, boss_name: str, hours: float):
    """보스 주기 변경. 예: /setboss 카스파 3"""
    if boss_name in boss_states:
        boss_states[boss_name]["interval_hours"] = hours
        # 레지스트리 주기 동기화 (전투봇 겹침 판정용)
        reg = rb_schedule.load_registry(_SCHED_REGISTRY_PATH).get("boss", {}).get(boss_name)
        if isinstance(reg, dict):
            reg["interval_hours"] = hours
            _publish_boss_schedule(boss_name, reg)
        await ctx.send(f"✅ **{boss_name}** 출현 주기를 **{hours}시간**으로 설정했습니다!")
    else:
        await ctx.send(f"❌ **{boss_name}** 스케줄이 실행 중이 아니에요. `/startboss`로 먼저 시작하세요.")


@bot.hybrid_command()
async def listboss(ctx):
    """현재 등록된 보스 스케줄 목록"""
    lines = []
    if boss_states:
        lines.append("**[주기 반복]**")
        for name, state in boss_states.items():
            icon = "🟢" if state["task"] and not state["task"].done() else "🔴"
            lines.append(f"  {icon} **{name}** — {state['interval_hours']}시간 주기")
    if boss_daily_states:
        lines.append("**[매일 고정]**")
        for name, time_tasks in boss_daily_states.items():
            cfg = DAILY_BOSSES_CONFIG.get(name, {})
            alert_min = cfg.get("alert_minutes", DEFAULT_ALERT_MINUTES)
            times_str = ", ".join(time_tasks.keys())
            lines.append(f"  🟢 **{name}** — {times_str} | {alert_min}분 전 알림")
    if not lines:
        await ctx.send("❌ 실행 중인 보스 스케줄이 없어요.")
        return
    await ctx.send("📋 **현재 보스 스케줄**\n" + "\n".join(lines))


@bot.hybrid_command()
async def setrefresh(ctx, time_str: str):
    """일일 정리 시각 변경. 예: /setrefresh 00:00"""
    global daily_refresh_hour, daily_refresh_minute, daily_refresh_task
    try:
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        daily_refresh_hour = hour
        daily_refresh_minute = minute
        if daily_refresh_task and not daily_refresh_task.done():
            daily_refresh_task.cancel()
        daily_refresh_task = asyncio.ensure_future(daily_refresh())
        await ctx.send(f"✅ 일일 정리 시각을 **{hour:02d}:{minute:02d}**로 변경했습니다!")
    except Exception:
        await ctx.send("❌ 형식이 잘못됐어요! 예: `/setrefresh 00:00`")


@bot.hybrid_command()
async def history(ctx, days: int = 7):
    text = get_history_text(days)
    if not text:
        await ctx.send("❌ 히스토리 데이터가 없어요!")
        return
    await ctx.send(text)


@bot.hybrid_command(name="stats", aliases=["통계", "참석통계"])
async def stats(ctx, period: str = "30", top: str = "25"):
    """참석자별 보스 참석 통계. 예: /stats 30 25 (최근 30일, 상위 25명)
    /stats all → 전체 기간"""
    if not os.path.exists(EXCEL_PATH):
        await ctx.send("❌ 저장된 데이터가 없어요!")
        return

    # 'all' 처리
    all_mode = False
    days = 30
    raw = str(period).lower().strip()
    if raw in ("all", "전체", "0"):
        all_mode = True
    else:
        try:
            days = int(raw)
        except Exception:
            await ctx.send("❌ 일수는 숫자 또는 `all` 이어야 해요. 예: `/stats 30` / `/stats all`")
            return
    # top: 숫자 또는 'all' (제한 해제). IPC 경유 시 문자열로 들어옴
    top_all = False
    top_raw = str(top).lower().strip()
    if top_raw in ("all", "전체", "0"):
        top_all = True
    else:
        try:
            top = int(top_raw)
        except Exception:
            top = 25

    # JSON 미러 우선 (7단계) — 실패 시 Excel 직접 읽기. 행 튜플 배치는 기존과 동일.
    rows_by_sheet = None
    try:
        from RaidBook_boss_data import _mirror as _bd_mirror
        m = _bd_mirror(EXCEL_PATH, system_sheets=SYSTEM_SHEETS)
        if m is not None:
            rows_by_sheet = {
                sn: [(r["msg_id"], r.get("event"), None, r.get("uid") or None,
                      None, r.get("name") or None) for r in recs]
                for sn, recs in m.iter_sheets(system_sheets=SYSTEM_SHEETS)
            }
    except Exception as e:
        log.info(f"[stats 미러 실패 → Excel 폴백] {e}")
        rows_by_sheet = None
    if rows_by_sheet is None:
        try:
            wb = load_workbook(EXCEL_PATH, read_only=True)
        except Exception as e:
            await ctx.send(f"❌ Excel 로드 실패: {e}")
            return
        rows_by_sheet = {
            s: list(wb[s].iter_rows(min_row=2, values_only=True))
            for s in wb.sheetnames if s not in SYSTEM_SHEETS
        }
        wb.close()

    # 대상 시트 필터 (날짜 시트만, 최근 N일)
    target_sheets = []
    if all_mode:
        target_sheets = list(rows_by_sheet.keys())
    else:
        cutoff = datetime.now().date() - timedelta(days=int(days) - 1)
        for s in rows_by_sheet.keys():
            try:
                d = datetime.strptime(s, "%Y-%m-%d").date()
                if d >= cutoff:
                    target_sheets.append(s)
            except Exception:
                continue

    if not target_sheets:
        await ctx.send("❌ 대상 기간에 데이터가 없어요!")
        return

    id_to_name = _load_id_to_name_mapping()
    counter = defaultdict(int)         # key: uid (alias 적용 후) → count
    per_boss = defaultdict(lambda: defaultdict(int))
    total_records = 0
    alias_merged = 0

    for sheet_name in target_sheets:
        for row in rows_by_sheet.get(sheet_name, []):
            if not row or not row[0]:
                continue
            boss_name = row[1] if len(row) > 1 else ""
            uid_val = row[3] if len(row) > 3 else None
            name_val = row[5] if len(row) > 5 else None
            key = None
            if uid_val:
                original = str(uid_val)
                key = _resolve_uid(original)   # alias 적용 + 로깅
                if key != original:
                    alias_merged += 1
            elif name_val:
                key = str(name_val)
            if not key:
                continue
            counter[key] += 1
            if boss_name:
                per_boss[key][str(boss_name)] += 1
            total_records += 1

    if alias_merged > 0:
        log.info(f"[stats] alias 통합 적용: {alias_merged}건 (동명이인/부계정 통합)")

    if not counter:
        await ctx.send("❌ 집계할 참석 데이터가 없어요!")
        return

    sorted_items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    if top_all:
        # 'all' → 제한 해제 (전원)
        pass
    else:
        top_n = max(1, int(top))
        sorted_items = sorted_items[:top_n]
    total_listed = len(sorted_items)

    period = "전체 기간" if all_mode else f"최근 {days}일"
    embed = discord.Embed(
        title=f"📊 보스 참석 통계 — {period}",
        description=f"대상 시트: **{len(target_sheets)}일** | 총 참석 기록: **{total_records:,}**건",
        color=discord.Color.gold(),
    )

    lines = []
    for rank, (key, cnt) in enumerate(sorted_items, start=1):
        # uid → 이름 매핑
        display = id_to_name.get(key, key)
        if display == key and key.isdigit():
            display = f"`{key[:6]}...`"
        # 가장 많이 참석한 보스 Top 3
        top_bosses = sorted(per_boss[key].items(), key=lambda x: x[1], reverse=True)[:3]
        bosses_str = ", ".join(f"{b}({c})" for b, c in top_bosses) if top_bosses else ""
        line = f"**{rank}.** {display} — **{cnt}회**"
        if bosses_str:
            line += f"  ｜ {bosses_str}"
        lines.append(line)

    # Discord embed description 4096자 제한 → 여러 embed로 페이지 분할 (전원 표시 시)
    PAGE_LIMIT = 3500
    pages = []
    cur_lines = []
    cur_len = len(embed.description) + 2
    for line in lines:
        if cur_len + len(line) + 1 > PAGE_LIMIT and cur_lines:
            pages.append(cur_lines)
            cur_lines = [line]
            cur_len = len(line) + 1
        else:
            cur_lines.append(line)
            cur_len += len(line) + 1
    if cur_lines:
        pages.append(cur_lines)

    total_pages = len(pages) or 1
    base_desc = embed.description
    for idx, page_lines in enumerate(pages, start=1):
        page_embed = discord.Embed(
            title=f"📊 보스 참석 통계 — {period}" + (f" ({idx}/{total_pages})" if total_pages > 1 else ""),
            description=base_desc + "\n\n" + "\n".join(page_lines),
            color=discord.Color.gold(),
        )
        label = "전원" if top_all else f"상위 {total_listed}명"
        page_embed.set_footer(text=f"{label} | /stats [일수|all] [상위N|all]")
        await ctx.send(embed=page_embed)
    if not pages:
        await ctx.send(embed=embed)


@bot.hybrid_command()
async def refresh(ctx):
    if not os.path.exists(EXCEL_PATH):
        await ctx.send("❌ 저장된 데이터가 없어요!")
        return
    try:
        await ctx.send("⏳ 데이터 업데이트 중...")
        guild_updated = await update_mapping_from_guild(ctx.guild) if ctx.guild else 0
        message_map = await build_message_map_from_channel()
        async with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            auto_migrate_excel(wb)
            fixed_count = fix_missing_ids(wb, message_map)
            for sheet_name in wb.sheetnames:
                if sheet_name not in SYSTEM_SHEETS:
                    reapply_raw_sheet_format(wb[sheet_name])
            mapped_count = apply_user_id_mapping(wb)
            update_history_sheet(wb)
            update_boss_attendance_sheet(wb)
            update_stats_sheet(wb)
            reorder_sheets(wb)
            save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
            if not save_ok:
                await ctx.send(f"❌ 데이터 저장 실패: {save_err}")
                return
            backup_excel()

        # 로그 기반 데이터 검증
        verified = 0
        try:
            verified = verify_and_fix_from_logs()
        except Exception as e:
            log.info(f"[로그 검증 오류] {e}")
            await ctx.send(f"⚠️ 로그 검증 중 오류가 발생했어요: {e}")

        # 검증 후 통계 재갱신
        if verified > 0:
            async with excel_lock:
                wb = load_workbook(EXCEL_PATH)
                update_history_sheet(wb)
                update_boss_attendance_sheet(wb)
                update_stats_sheet(wb)
                reorder_sheets(wb)
                save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
                if not save_ok:
                    await ctx.send(f"❌ 로그 검증 보정 저장 실패: {save_err}")
                    return
                backup_excel()

        status_parts = []
        if guild_updated > 0:
            status_parts.append(f"서버 멤버 {guild_updated}명 매핑 갱신")
        if fixed_count > 0:
            status_parts.append(f"ID {fixed_count}개 발급")
        if verified > 0:
            status_parts.append(f"로그 검증 {verified}건 보정")
        if mapped_count > 0:
            status_parts.append(f"유저 ID {mapped_count}건 매핑")
        status_msg = f" ({', '.join(status_parts)})" if status_parts else ""
        await ctx.send(f"✅ 모든 데이터가 새 서식으로 업데이트됐어요!{status_msg}")
    except Exception as e:
        await ctx.send(f"❌ 업데이트 실패: {e}")


def _parse_date_arg(date_str):
    """yymmddhh(8자리) → (sheet_name '2026-05-26', time_str '20:00') 또는 None"""
    if len(date_str) != 8 or not date_str.isdigit():
        return None, None
    yy, mm, dd, hh = date_str[:2], date_str[2:4], date_str[4:6], date_str[6:8]
    try:
        dt = datetime(2000 + int(yy), int(mm), int(dd), int(hh), 0)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        return None, None


def _find_boss_record_by_id(msg_id):
    """메시지 ID로 보스 기록 검색.
    active_messages.json(활성 메시지) → Excel(마감된 메시지) 순으로 탐색.
    반환: (msg_id, boss_name, boss_time, sheet_name, attendees) 또는 None
    """
    # ── 1순위: 활성 메시지 JSON ─────────────────────────────────────────────
    # close_minutes > 0 환경에서는 마감 전 Excel에 저장되지 않으므로 JSON을 먼저 확인
    active = load_active_messages()
    info = active.get(str(msg_id))
    if info:
        attendees = [str(u) for u in info.get("attendees", [])]
        sheet_name = info.get("created_date", datetime.now().strftime("%Y-%m-%d"))
        return (str(msg_id), info.get("boss_name", ""), info.get("boss_time", ""), sheet_name, attendees)

    # ── 2순위: Excel (마감된 메시지) ──────────────────────────────────────────
    if not os.path.exists(EXCEL_PATH):
        return None
    wb = load_workbook(EXCEL_PATH)
    for sn in wb.sheetnames:
        if sn in SYSTEM_SHEETS:
            continue
        ws = wb[sn]
        data = {"boss_name": "", "boss_time": "", "attendees": []}
        found = False
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row[0] or str(row[0]) != msg_id:
                continue
            found = True
            data["boss_name"] = row[1] or ""
            data["boss_time"] = row[2] or ""
            if row[3]:
                data["attendees"].append(str(row[3]))
        if found:
            wb.close()
            return (msg_id, data["boss_name"], data["boss_time"], sn, data["attendees"])
    wb.close()
    return None


def _find_boss_record(boss_name, sheet_name, target_time):
    """Excel에서 보스명+날짜+시간으로 기록 검색. (msg_id, boss_time, attendees) 또는 None"""
    if not os.path.exists(EXCEL_PATH):
        return None
    wb = load_workbook(EXCEL_PATH)
    try:
        th, tm = map(int, target_time.split(":"))
        target_minutes = th * 60 + tm
    except Exception:
        target_minutes = None

    best = None
    best_diff = float("inf")

    sheets = [sheet_name] if sheet_name in wb.sheetnames else [
        s for s in wb.sheetnames if s not in SYSTEM_SHEETS
    ]
    for sn in sheets:
        ws = wb[sn]
        rows_by_id = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row[0] or not row[1]:
                continue
            mid = str(row[0])
            if row[1] != boss_name:
                continue
            if mid not in rows_by_id:
                rows_by_id[mid] = {"boss_time": row[2] or "", "attendees": []}
            if row[3]:
                rows_by_id[mid]["attendees"].append(row[3])

        for mid, data in rows_by_id.items():
            bt = data["boss_time"]
            diff = float("inf")
            if target_minutes is not None and bt:
                try:
                    bh, bm = map(int, bt.split(":"))
                    diff = abs(bh * 60 + bm - target_minutes)
                except Exception:
                    pass
            if diff < best_diff:
                best_diff = diff
                best = (mid, bt, sn, data["attendees"])

    wb.close()
    return best  # (msg_id, boss_time, sheet_name, attendees)


class _AttendeeEditView(discord.ui.View):
    def __init__(self, action, msg_id, boss_name, boss_time, sheet_name, attendee_uid, attendee_display, current_attendees):
        super().__init__(timeout=60)
        self.action = action
        self.msg_id = msg_id
        self.boss_name = boss_name
        self.boss_time = boss_time
        self.sheet_name = sheet_name
        self.attendee_uid = attendee_uid        # UID (Excel col4)
        self.attendee_display = attendee_display  # 표시용 이름 (Excel col6)
        self.current_attendees = current_attendees

    @discord.ui.button(label="확인", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            async with excel_lock:
                wb = load_workbook(EXCEL_PATH)
                ws = wb[self.sheet_name] if self.sheet_name in wb.sheetnames else None
                if ws is None:
                    await interaction.followup.send("❌ 시트를 찾을 수 없어요.")
                    self.stop()
                    return

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                date_log = self.sheet_name.replace("-", "")

                if self.action == "add":
                    next_row = ws.max_row + 1
                    ws.cell(row=next_row, column=1, value=self.msg_id).number_format = "@"
                    ws.cell(row=next_row, column=2, value=self.boss_name)
                    ws.cell(row=next_row, column=3, value=self.boss_time)
                    ws.cell(row=next_row, column=4, value=self.attendee_uid)   # col4 = UID
                    ws.cell(row=next_row, column=5, value=now_str)
                    ws.cell(row=next_row, column=6, value=self.attendee_display)  # col6 = 이름
                    new_total = len(self.current_attendees) + 1
                    log.info(rb_att.attend_log_line("참석", f"{self.boss_name} {self.boss_time}",
                                                    self.msg_id, self.attendee_display,
                                                    self.attendee_uid, new_total))
                else:  # remove
                    for row_idx in range(ws.max_row, 1, -1):
                        row_data = [ws.cell(row=row_idx, column=c).value for c in range(1, 7)]
                        if str(row_data[0]) == self.msg_id and str(row_data[3]) == str(self.attendee_uid):
                            ws.delete_rows(row_idx)
                            break
                    new_total = max(0, len(self.current_attendees) - 1)
                    log.info(rb_att.attend_log_line("취소", f"{self.boss_name} {self.boss_time}",
                                                    self.msg_id, self.attendee_display,
                                                    self.attendee_uid, new_total))

                apply_border_all(ws)
                auto_column_width(ws)
                update_history_sheet(wb)
                update_boss_attendance_sheet(wb)
                update_stats_sheet(wb)
                reorder_sheets(wb)
                save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
                if not save_ok:
                    await interaction.followup.send(f"❌ 저장 실패: {save_err}")
                    self.stop()
                    return
                backup_excel()

            # ── active_messages.json 동기화 ──────────────────────────────────
            # 수동 추가/삭제 후 JSON이 구버전이면 이후 버튼 클릭 시 덮어씌워지는 문제 방지
            updated_attendees = None
            try:
                with active_messages_lock:
                    active = load_active_messages()
                    if self.msg_id in active:
                        current = list(active[self.msg_id].get("attendees", []))
                        uid_str = str(self.attendee_uid)
                        if self.action == "add":
                            if uid_str not in current:
                                current.append(uid_str)
                        else:
                            if uid_str in current:
                                current.remove(uid_str)
                        active[self.msg_id]["attendees"] = current
                        write_active_messages(active)
                        updated_attendees = current
                        log.info(f"[수동조정-JSON] {self.action} {self.attendee_display} → active_messages 동기화 완료 (총 {len(current)}명)")
            except Exception as je:
                log.info(f"[수동조정-JSON 동기화 실패] {je}")

            # ── Discord 메시지 embed 갱신 ─────────────────────────────────────
            try:
                active = load_active_messages()
                msg_info = active.get(self.msg_id)
                is_active = msg_info is not None
                # 마감된 메시지(JSON에 없음)는 ALERT_CHANNEL_ID 폴백 사용
                channel_id = int(msg_info["channel_id"]) if msg_info else ALERT_CHANNEL_ID

                # 참석자 목록: JSON 동기화 결과 우선, 없으면 Excel에서 재조회
                if updated_attendees is None:
                    updated_attendees = [str(u) for u in load_attendees_from_excel(int(self.msg_id))]

                id_to_name = _load_id_to_name_mapping()
                names = ", ".join(id_to_name.get(u, u) for u in updated_attendees) if updated_attendees else "없음"

                loot_text = ""
                loot_acq = ""
                disable_time = None
                if msg_info:
                    loot_text = msg_info.get("loot", "") or ""
                    loot_acq = msg_info.get("loot_acquirers", "") or load_loot_acquirers_from_excel(int(self.msg_id)) or ""
                    disable_time = _coerce_disable_time(msg_info.get("disable_time"))
                    image_url = msg_info.get("image_url", "") or ""
                    image_filename = msg_info.get("image_filename", "") or ""
                else:
                    loot_text, _, _ = load_loot_from_excel(int(self.msg_id))
                    loot_acq = load_loot_acquirers_from_excel(int(self.msg_id)) or ""
                    image_url = ""
                    image_filename = ""

                if channel_id:
                    ch = bot.get_channel(channel_id)
                    if ch:
                        target_msg = await ch.fetch_message(int(self.msg_id))
                        msg_image_filename, msg_image_url = _message_image_ref(target_msg)
                        image_filename = msg_image_filename or image_filename
                        image_url = msg_image_url or image_url
                        image_file, image_kwargs = _boss_image_file_and_kwargs(self.boss_name, target_msg)
                        embed = make_attend_embed(
                            self.boss_name, len(updated_attendees), names,
                            self.boss_time, expired=not is_active,
                            loot=loot_text, loot_acquirers=loot_acq, disable_time=disable_time,
                            **image_kwargs,
                        )
                        await _edit_message_with_image(
                            target_msg,
                            embed=embed,
                            image_file=image_file,
                            content=_boss_alert_content(self.boss_name, self.boss_time, expired=not is_active),
                        )
                        log.info(f"[수동조정-embed 갱신] {self.boss_name} {self.boss_time} | 참석 {len(updated_attendees)}명")
            except Exception as me:
                log.info(f"[수동조정-embed 갱신 실패] {me}")

            verb = "추가" if self.action == "add" else "제거"
            await interaction.followup.send(
                f"✅ **{self.attendee_display}**님을 **{self.boss_name} {self.boss_time}** 참석자에서 {verb}했어요."
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 처리 실패: {e}")
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ 취소됐어요.", view=None, embed=None)
        self.stop()


async def _resolve_boss_record(ctx, args):
    """
    args 2개: (msg_id, attendee_name)
    args 3개: (boss_name, date_str, attendee_name)
    반환: (record, attendee_name) 또는 (None, None)
    """
    if len(args) == 2:
        msg_id, attendee_name = args[0], args[1]
        if not msg_id.isdigit():
            await ctx.send("❌ 메시지 ID는 숫자여야 해요. 예: `/add 1505112567712972881 조조야`")
            return None, None
        record = _find_boss_record_by_id(msg_id)
        if not record:
            await ctx.send(f"❌ 메시지 ID `{msg_id}` 기록을 찾을 수 없어요.")
            return None, None
        return record, attendee_name
    elif len(args) == 3:
        boss_name, date_str, attendee_name = args
        sheet_name, target_time = _parse_date_arg(date_str)
        if not sheet_name:
            await ctx.send("❌ 날짜 형식이 잘못됐어요. 예: `26052620` (26년 5월 26일 20시)")
            return None, None
        record = _find_boss_record(boss_name, sheet_name, target_time)
        if not record:
            await ctx.send(f"❌ **{boss_name}** 기록을 찾을 수 없어요. (`{sheet_name}` 날짜 확인)")
            return None, None
        return record, attendee_name
    else:
        await ctx.send("❌ 사용법:\n`/add 보스명 yymmddhh 유저명`\n`/add 메시지ID 유저명`")
        return None, None


@bot.hybrid_command()
async def add(ctx, *, args: str = ""):
    """참석자 수동 추가. 예: /add 카스파 26052620 조조야 또는 /add 1505112567712972881 조조야"""
    record, attendee_name = await _resolve_boss_record(ctx, tuple(args.split()))
    if record is None:
        return

    msg_id, boss_name, boss_time, found_sheet, attendees = record

    # 이름 → UID 변환 (매핑에 없으면 그대로 사용)
    n2u = _name_to_uid_mapping()
    attendee_uid = n2u.get(attendee_name, attendee_name)
    id2n = _load_id_to_name_mapping()
    attendee_display = id2n.get(str(attendee_uid), attendee_name)

    if attendee_uid in attendees:
        await ctx.send(f"⚠️ **{attendee_display}**님은 이미 참석자에 포함되어 있어요.\n현재 참석자: {', '.join(id2n.get(u, u) for u in attendees)}")
        return

    embed = discord.Embed(title="참석자 추가 확인", color=discord.Color.green())
    embed.add_field(name="보스", value=f"{boss_name} | {boss_time}", inline=True)
    embed.add_field(name="날짜", value=found_sheet, inline=True)
    embed.add_field(name="추가할 유저", value=attendee_display, inline=False)
    embed.add_field(name="현재 참석자", value=", ".join(id2n.get(u, u) for u in attendees) if attendees else "없음", inline=False)

    view = _AttendeeEditView("add", msg_id, boss_name, boss_time, found_sheet, attendee_uid, attendee_display, attendees)
    await ctx.send(embed=embed, view=view)


@bot.hybrid_command()
async def remove(ctx, *, args: str = ""):
    """참석자 수동 제거. 예: /remove 카스파 26052620 조조야 또는 /remove 1505112567712972881 조조야"""
    record, attendee_name = await _resolve_boss_record(ctx, tuple(args.split()))
    if record is None:
        return

    msg_id, boss_name, boss_time, found_sheet, attendees = record

    # 이름 → UID 변환
    n2u = _name_to_uid_mapping()
    attendee_uid = n2u.get(attendee_name, attendee_name)
    id2n = _load_id_to_name_mapping()
    attendee_display = id2n.get(str(attendee_uid), attendee_name)

    if attendee_uid not in attendees:
        await ctx.send(f"⚠️ **{attendee_display}**님은 참석자 목록에 없어요.\n현재 참석자: {', '.join(id2n.get(u, u) for u in attendees) if attendees else '없음'}")
        return

    embed = discord.Embed(title="참석자 제거 확인", color=discord.Color.red())
    embed.add_field(name="보스", value=f"{boss_name} | {boss_time}", inline=True)
    embed.add_field(name="날짜", value=found_sheet, inline=True)
    embed.add_field(name="제거할 유저", value=attendee_display, inline=False)
    embed.add_field(name="현재 참석자", value=", ".join(id2n.get(u, u) for u in attendees), inline=False)

    view = _AttendeeEditView("remove", msg_id, boss_name, boss_time, found_sheet, attendee_uid, attendee_display, attendees)
    await ctx.send(embed=embed, view=view)


@bot.hybrid_command()
async def fixid(ctx):
    if not os.path.exists(EXCEL_PATH):
        await ctx.send("❌ 저장된 데이터가 없어요!")
        return
    try:
        await ctx.send("⏳ Discord 채널 스캔 + ID 발급 중...")
        message_map = await build_message_map_from_channel()
        async with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            auto_migrate_excel(wb)
            fixed_count = fix_missing_ids(wb, message_map)
            if fixed_count == 0:
                await ctx.send("✅ ID 없는 데이터가 없어요!")
                return
            update_history_sheet(wb)
            update_boss_attendance_sheet(wb)
            update_stats_sheet(wb)
            reorder_sheets(wb)
            save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
            if not save_ok:
                await ctx.send(f"❌ 저장 실패: {save_err}")
                return
            backup_excel()
        await ctx.send(f"✅ **{fixed_count}개** 데이터에 ID 발급 완료!")
    except Exception as e:
        await ctx.send(f"❌ 실패: {e}")


@bot.hybrid_command()
async def reopen(ctx, date_time_str: str, minutes: int = 20):
    """마감된 알림을 다시 활성화. 예: /reopen 2605161700 또는 /reopen 2605161700 30"""
    message_id, boss_name, boss_time = find_message_id_by_datetime(date_time_str)
    if not message_id:
        await ctx.send(f"❌ 해당 날짜/시간의 데이터를 찾을 수 없어요! (입력: {date_time_str})")
        return

    try:
        msg = await bot.get_channel(ALERT_CHANNEL_ID).fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    attendees = load_attendees_from_excel(int(message_id))
    boss_name = boss_name or "알 수 없음"
    boss_time = boss_time or "알 수 없음"
    loot, _, loot_announcement_id = load_loot_from_excel(int(message_id))

    disable_time = datetime.now() + timedelta(minutes=minutes)
    attend_view = AttendView(
        message_id=int(message_id),
        boss_name=boss_name,
        boss_time=boss_time,
        created_date=datetime.now().strftime("%Y-%m-%d"),
        loot=loot,
        disable_time=disable_time,
    )
    attend_view.attendees = list(attendees)
    attend_view.loot_announcement_id = str(loot_announcement_id or "")
    attend_view.loot_acquirers = load_loot_acquirers_from_excel(int(message_id)) or ""
    image_filename, image_url = _message_image_ref(msg)
    attend_view.image_filename = image_filename
    attend_view.image_url = image_url

    names = ", ".join(attend_view.attendees) if attend_view.attendees else "없음"
    image_file, image_kwargs = _boss_image_file_and_kwargs(boss_name, msg)
    embed = make_attend_embed(
        boss_name, len(attend_view.attendees), names, boss_time,
        loot=attend_view.loot,
        disable_time=disable_time,
        loot_acquirers=getattr(attend_view, "loot_acquirers", ""),
        **image_kwargs,
    )
    await _edit_message_with_image(
        msg,
        embed=embed,
        view=attend_view,
        image_file=image_file,
        content=_boss_alert_content(boss_name, boss_time),
    )

    save_active_message(int(message_id), ALERT_CHANNEL_ID, boss_name, boss_time,
                        disable_time.strftime("%Y-%m-%d %H:%M:%S"),
                        created_date=datetime.now().strftime("%Y-%m-%d"),
                        attendees=attend_view.attendees,
                        loot=attend_view.loot,
                        loot_announcement_id=attend_view.loot_announcement_id,
                        image_url=attend_view.image_url,
                        image_filename=attend_view.image_filename)

    async def auto_disable():
        delay = (disable_time - datetime.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await attend_view.disable_buttons(msg)
            log.info(f"[자동 마감] {boss_name} {boss_time} | 참석: {len(attend_view.attendees)}명")
        except Exception:
            pass
        remove_active_message(int(message_id))

    asyncio.ensure_future(auto_disable())
    log.info(f"[재활성화] {boss_name} {boss_time} | {minutes}분 | 메시지 ID: {message_id}")
    await ctx.send(f"✅ **{boss_name}** 알림을 다시 활성화했어요! ({minutes}분 후 마감)")


@bot.hybrid_command()
async def rename(ctx, old_name: str, new_name: str):
    """참석자 이름 일괄 변경. 예: /rename 기존닉네임 새닉네임"""
    if not os.path.exists(EXCEL_PATH):
        await ctx.send("❌ 저장된 데이터가 없어요!")
        return
    try:
        await ctx.send(f"⏳ **{old_name}** → **{new_name}** 변경 중...")
        async with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            changed_count = 0

            for sheet_name in wb.sheetnames:
                if sheet_name in SYSTEM_SHEETS:
                    continue
                ws = wb[sheet_name]
                for row in ws.iter_rows(min_row=2):
                    # 참석자 컬럼 (4번째, index 3)
                    cell = row[3]
                    if cell.value == old_name:
                        cell.value = new_name
                        changed_count += 1

            if changed_count == 0:
                await ctx.send(f"❌ **{old_name}** 데이터를 찾을 수 없어요!")
                return

            update_history_sheet(wb)
            update_boss_attendance_sheet(wb)
            update_stats_sheet(wb)
            reorder_sheets(wb)
            save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
            if not save_ok:
                await ctx.send(f"❌ 저장 실패: {save_err}")
                return
            backup_excel()
            await ctx.send(f"✅ **{changed_count}개** 데이터에서 **{old_name}** → **{new_name}** 변경 완료!")
    except Exception as e:
        await ctx.send(f"❌ 변경 실패: {e}")


@bot.hybrid_command()
async def restart(ctx, *, flags: str = ""):
    """데이터 저장/갱신 후 봇을 재시작합니다.
    예: /restart           → 디스코드 알림 표시 + 재시작
        /restart silent    → 디스코드 알림 없이 조용히 재시작 (quiet/s 도 가능)"""
    silent = any(str(f).lower() in ("silent", "quiet", "s", "조용", "조용히") for f in flags.split())
    async def _say(msg):
        if not silent:
            try: await ctx.send(msg)
            except Exception: pass
    log.info(f"[restart] silent={silent} | 시작")
    await _say(f"🔄 **데이터 저장 후 재시작합니다...** `{datetime.now().strftime('%Y-%m-%d %H:%M')}`")
    if os.path.exists(EXCEL_PATH):
        message_map = await build_message_map_from_channel()
        async with excel_lock:
            try:
                wb = load_workbook(EXCEL_PATH)
                auto_migrate_excel(wb)
                fix_missing_ids(wb, message_map)
                for sheet_name in wb.sheetnames:
                    if sheet_name not in SYSTEM_SHEETS:
                        reapply_raw_sheet_format(wb[sheet_name])
                update_history_sheet(wb)
                update_boss_attendance_sheet(wb)
                update_stats_sheet(wb)
                reorder_sheets(wb)
                save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
                if save_ok:
                    backup_excel()
                else:
                    await _say(f"⚠️ 데이터 저장 실패: {save_err}")
                    return
                await _say("✅ 데이터 저장 완료!")
            except Exception as e:
                await _say(f"⚠️ 데이터 저장 실패: {e}")
    await bot.close()
    # Watchdog 관리 중이면 깨끗이 종료 → Watchdog가 재시작 (execv로 자가 재시작 시
    # Watchdog 통제 밖에서 프로세스가 갈려 중복/고아가 생길 수 있으므로 회피).
    # 단독 실행(standalone)일 때만 execv로 자가 재시작.
    if os.environ.get("RAIDBOOK_MANAGED") == "1":
        log.info("[restart] Watchdog 관리 모드 — 종료 후 Watchdog 재시작에 위임")
        os._exit(0)
    else:
        log.info("[restart] 단독 실행 모드 — execv 자가 재시작")
        os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.hybrid_command()
async def save(ctx):
    if not os.path.exists(EXCEL_PATH):
        await ctx.send("❌ 저장된 데이터가 없어요!")
        return
    try:
        message_map = await build_message_map_from_channel()
        async with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            auto_migrate_excel(wb)
            fix_missing_ids(wb, message_map)
            for sheet_name in wb.sheetnames:
                if sheet_name not in SYSTEM_SHEETS:
                    reapply_raw_sheet_format(wb[sheet_name])
            update_history_sheet(wb)
            update_boss_attendance_sheet(wb)
            update_stats_sheet(wb)
            reorder_sheets(wb)
            save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
            if not save_ok:
                await ctx.send(f"❌ 저장 실패: {save_err}")
                return
            backup_excel()
    except Exception as e:
        await ctx.send(f"❌ 데이터 저장 실패: {e}")
        return

    try:
        await ctx.send(
            "✅ 보스 참석 데이터 엑셀이에요! 클릭해서 다운로드하세요.",
            file=discord.File(EXCEL_PATH)
        )
    except Exception as e:
        await ctx.send(f"❌ 파일 전송 실패(데이터 저장은 완료됨): {e}")


@bot.hybrid_command()
async def create(ctx, boss_name: str, boss_time_str: str, minutes: int = 20):
    """수동 참석 알림 생성. 예: /create 카스파 17:00 또는 /create 카스파 17:00 60"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return

    created_date = datetime.now().strftime("%Y-%m-%d")
    disable_time = datetime.now() + timedelta(minutes=minutes)
    attend_view = AttendView(boss_name=boss_name, boss_time=boss_time_str, created_date=created_date, disable_time=disable_time)
    msg = await channel.send(
        content=_boss_alert_content(boss_name, boss_time_str),
        embed=make_attend_embed(boss_name, 0, "없음", boss_time_str, disable_time=disable_time),
        view=attend_view,
    )
    attend_view.message_id = msg.id

    save_active_message(msg.id, channel.id, boss_name, boss_time_str,
                        disable_time.strftime("%Y-%m-%d %H:%M:%S"), created_date=created_date)
    try:
        _schedule_voice_check(msg.id, datetime.strptime(f"{created_date} {boss_time_str}", "%Y-%m-%d %H:%M"))
    except ValueError:
        pass

    async def auto_disable():
        delay = (disable_time - datetime.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await attend_view.disable_buttons(msg)
            log.info(f"[자동 마감] {boss_name} {boss_time_str} | 참석: {len(attend_view.attendees)}명")
        except Exception:
            pass
        remove_active_message(msg.id)

    asyncio.ensure_future(auto_disable())
    log.info(f"[수동 생성] {boss_name} {boss_time_str} | {minutes}분 후 마감 | 메시지 ID: {msg.id}")
    await ctx.send(f"✅ **{boss_name}** {boss_time_str} 참석 알림을 생성했어요! ({minutes}분 후 마감)")


@bot.hybrid_command()
async def recover(ctx, message_id: str):
    """마감된 메시지에서 참석자를 읽어 Excel에 복구. 예: /recover 1504994786195538101"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return

    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    if not msg.embeds:
        await ctx.send("❌ embed가 없는 메시지예요!")
        return

    # embed에서 파싱
    embed = msg.embeds[0]
    desc = embed.description or ""
    title = embed.title or ""
    boss_name = ""
    boss_time = ""
    attendees = []

    if "보스 출현 알림" in title:
        boss_name = title.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
    for line in desc.split("\n"):
        if "보스 출현 알림" in line:
            boss_name = line.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
        elif "보스 리젠 시각" in line:
            boss_time = line.split("**")[-2].strip() if "**" in line else ""
        elif "참석:" in line:
            parts = line.split("|")
            if len(parts) > 1:
                names_str = parts[-1].strip()
                if names_str and names_str != "없음":
                    attendees = [n.strip() for n in names_str.split(",") if n.strip()]

    if not boss_name:
        boss_name = "알 수 없음"
    if not boss_time:
        boss_time = "알 수 없음"

    # 메시지 생성 날짜 (Discord snowflake → timestamp)
    discord_epoch = 1420070400000
    timestamp_ms = (int(message_id) >> 22) + discord_epoch
    created_date = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")

    await ctx.send(
        f"🔍 **복구 대상**\n"
        f"  보스: {boss_name} | 시간: {boss_time}\n"
        f"  날짜: {created_date}\n"
        f"  참석자 ({len(attendees)}명): {', '.join(attendees) if attendees else '없음'}\n\n"
        f"Excel에 저장합니다..."
    )

    await save_boss_attendees_to_excel(int(message_id), boss_name, boss_time, attendees, created_date)
    log.info(f"[복구] {boss_name} {boss_time} | {created_date} | 참석자: {', '.join(attendees) if attendees else '없음'}")
    await ctx.send(f"✅ **{boss_name}** {boss_time} 데이터를 Excel에 복구했어요! ({len(attendees)}명)")


@bot.hybrid_command()
async def sync(ctx, message_id: str):
    """Excel 데이터 기준으로 마감된 메시지의 embed를 업데이트. 예: /sync 메시지ID"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return

    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    attendees = load_attendees_from_excel(int(message_id))
    boss_name = load_boss_name_from_excel(int(message_id))
    boss_time = load_boss_time_from_excel(int(message_id))

    if not boss_name:
        await ctx.send(f"❌ Excel에 해당 메시지 데이터가 없어요! (ID: {message_id})")
        return

    id_to_name = _load_id_to_name_mapping()
    names = ", ".join(id_to_name.get(str(u), str(u)) for u in attendees) if attendees else "없음"
    loot_text, _, _ = load_loot_from_excel(int(message_id))
    loot_acq = load_loot_acquirers_from_excel(int(message_id))
    image_file, image_kwargs = _boss_image_file_and_kwargs(boss_name, msg)
    embed = make_attend_embed(
        boss_name, len(attendees), names, boss_time or "알 수 없음",
        expired=True, loot=loot_text or "", loot_acquirers=loot_acq or "",
        **image_kwargs,
    )
    try:
        await _edit_message_with_image(
            msg,
            embed=embed,
            view=None,
            image_file=image_file,
            content=_boss_alert_content(boss_name, boss_time, expired=True),
        )
        log.info(f"[동기화] {boss_name} {boss_time} | 참석: {len(attendees)}명 | 메시지 ID: {message_id}")
        await ctx.send(f"✅ **{boss_name}** {boss_time} 메시지를 Excel 데이터로 업데이트했어요! ({len(attendees)}명)")
    except Exception as e:
        await ctx.send(f"❌ 메시지 업데이트 실패: {e}")


@bot.hybrid_command()
async def attachloot(ctx, target: str = None, days: str = "7"):
    """획득 아이템 버튼이 없는 기존 보스 메시지에 버튼을 부착.
    예: /attachloot 1504994786195538101   (단일 메시지)
        /attachloot all 30                (최근 30일 전체 스캔)
    """
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return

    if not target:
        await ctx.send("사용법: `/attachloot <메시지ID>` 또는 `/attachloot all [일수]`")
        return

    # watchdog IPC 경유 시 days 가 문자열로 들어옴 → 안전 변환
    try:
        days = int(days)
    except Exception:
        await ctx.send("❌ 일수는 숫자여야 해요. 예: `/attachloot all 30`")
        return

    async def _attach_one(msg):
        """단일 메시지에 AttendView 부착. 결과: ok / skip / no_data / fail"""
        # 1) 이미 boss_party 버튼까지 있으면 skip (최신 view 부착 완료 기준)
        try:
            for row in msg.components:
                for comp in getattr(row, "children", []):
                    if getattr(comp, "custom_id", "") == "boss_party":
                        return "skip"
        except Exception:
            pass

        mid = msg.id
        # 2) JSON(활성) → Excel(마감) 순으로 데이터 복구
        active = load_active_messages()
        info = active.get(str(mid))
        if info:
            boss_name = info.get("boss_name", "")
            boss_time = info.get("boss_time", "")
            attendees = list(info.get("attendees", []))
            created_date = info.get("created_date")
            loot = info.get("loot", "") or ""
            loot_announcement_id = (
                info.get("loot_announcement_id", "")
                or info.get("announcement_msg_id", "")
                or ""
            )
            disable_time = info.get("disable_time")
            expired = False
        else:
            attendees = load_attendees_from_excel(mid) or []
            boss_name = load_boss_name_from_excel(mid) or ""
            boss_time = load_boss_time_from_excel(mid) or ""
            loot_text, _, loot_announcement_id = load_loot_from_excel(mid)
            loot = loot_text or ""
            created_date = None
            disable_time = None
            expired = True

        if not boss_name:
            return "no_data"

        # 3) AttendView 생성 후 부착 (embed는 유지)
        view = AttendView(
            message_id=mid, boss_name=boss_name, boss_time=boss_time,
            created_date=created_date, loot=loot, expired=expired,
            disable_time=disable_time,
        )
        view.attendees = attendees
        view.loot_announcement_id = str(loot_announcement_id or "")
        try:
            await msg.edit(view=view)
            return "ok"
        except Exception as e:
            log.info(f"[attachloot 실패] {mid} | {e}")
            return "fail"

    # ── 일괄 처리 ──
    if target.lower() == "all":
        status = await ctx.send(f"⏳ 최근 {days}일 메시지 스캔 중...")
        cutoff = datetime.now() - timedelta(days=days)
        ok_cnt = skip_cnt = no_data_cnt = fail_cnt = 0
        try:
            async for msg in channel.history(limit=2000, after=cutoff):
                if msg.author.id != bot.user.id or not msg.embeds:
                    continue
                desc = (msg.embeds[0].description or "")
                if "보스 출현 알림" not in desc:
                    continue
                result = await _attach_one(msg)
                if result == "ok":
                    ok_cnt += 1
                    log.info(f"[loot버튼 부착] {msg.id}")
                elif result == "skip":
                    skip_cnt += 1
                elif result == "no_data":
                    no_data_cnt += 1
                else:
                    fail_cnt += 1
                await asyncio.sleep(0.3)  # rate limit 보호
        except Exception as e:
            await status.edit(content=f"❌ 스캔 중 오류: {e}")
            return
        await status.edit(content=(
            f"✅ 부착 **{ok_cnt}** / 이미있음 {skip_cnt} / "
            f"데이터없음 {no_data_cnt} / 실패 {fail_cnt}"
        ))
        return

    # ── 단일 처리 ──
    try:
        msg = await channel.fetch_message(int(target))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {target})")
        return
    result = await _attach_one(msg)
    if result == "ok":
        await ctx.send(f"✅ 메시지 `{target}` 에 **획득 아이템** 버튼 부착 완료!")
    elif result == "skip":
        await ctx.send(f"ℹ️ 이미 획득 아이템 버튼이 있는 메시지에요. (ID: {target})")
    elif result == "no_data":
        await ctx.send(f"❌ Excel/JSON 에 해당 메시지 데이터가 없어요. (ID: {target})")
    else:
        await ctx.send(f"❌ 부착 실패. 로그를 확인해주세요. (ID: {target})")


@bot.hybrid_command()
async def close(ctx, message_id: str):
    """활성 메시지를 수동 마감. 예: /close 메시지ID"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return
    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    active = load_active_messages()
    info = active.get(message_id)
    if not info:
        await ctx.send(f"❌ 활성 메시지가 아니에요! (ID: {message_id})")
        return

    attend_view = AttendView(
        message_id=int(message_id),
        boss_name=info["boss_name"],
        boss_time=info["boss_time"],
        created_date=info.get("created_date"),
        loot=info.get("loot", ""),
    )
    attend_view.attendees = list(info.get("attendees", []))
    attend_view.loot_announcement_id = (
        info.get("loot_announcement_id", "")
        or info.get("announcement_msg_id", "")
        or ""
    )
    attend_view.image_url = info.get("image_url", "")
    attend_view.image_filename = info.get("image_filename", "")
    await attend_view.disable_buttons(msg)
    remove_active_message(int(message_id))
    log.info(f"[수동 마감] {info['boss_name']} {info['boss_time']} | 참석: {len(attend_view.attendees)}명")
    await ctx.send(f"✅ **{info['boss_name']}** {info['boss_time']} 수동 마감 완료! ({len(attend_view.attendees)}명)")


@bot.hybrid_command()
async def activate(ctx, message_id: str, minutes: int = 0):
    """마감된 메시지를 수동 활성화. 예: /activate 메시지ID 또는 /activate 메시지ID 30 (0이면 무기한)"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return
    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    active = load_active_messages()
    info = active.get(message_id)
    if info:
        attendees = list(info.get("attendees", []))
        boss_name = info.get("boss_name", "")
        boss_time = info.get("boss_time", "")
        loot = info.get("loot", "") or ""
        loot_announcement_id = (
            info.get("loot_announcement_id", "")
            or info.get("announcement_msg_id", "")
            or ""
        )
    else:
        attendees = load_attendees_from_excel(int(message_id))
        boss_name = load_boss_name_from_excel(int(message_id)) or "알 수 없음"
        boss_time = load_boss_time_from_excel(int(message_id)) or "알 수 없음"
        loot, _, loot_announcement_id = load_loot_from_excel(int(message_id))

    # 마감 시간 0이면 무기한 (9999분 = 약 7일)
    disable_minutes = minutes if minutes > 0 else 9999
    disable_time = datetime.now() + timedelta(minutes=disable_minutes)

    attend_view = AttendView(
        message_id=int(message_id),
        boss_name=boss_name,
        boss_time=boss_time,
        created_date=datetime.now().strftime("%Y-%m-%d"),
        loot=loot,
        disable_time=disable_time if minutes > 0 else None,
    )
    attend_view.attendees = list(attendees)
    attend_view.loot_announcement_id = str(loot_announcement_id or "")
    attend_view.loot_acquirers = (info.get("loot_acquirers", "") if info else "") or load_loot_acquirers_from_excel(int(message_id)) or ""
    image_filename, image_url = _message_image_ref(msg)
    attend_view.image_filename = image_filename or (info.get("image_filename", "") if info else "")
    attend_view.image_url = image_url or (info.get("image_url", "") if info else "")

    names = ", ".join(attend_view.attendees) if attend_view.attendees else "없음"
    image_file, image_kwargs = _boss_image_file_and_kwargs(boss_name, msg)
    embed = make_attend_embed(
        boss_name, len(attend_view.attendees), names, boss_time,
        loot=attend_view.loot,
        disable_time=(disable_time if minutes > 0 else None),
        loot_acquirers=getattr(attend_view, "loot_acquirers", ""),
        **image_kwargs,
    )
    await _edit_message_with_image(
        msg,
        embed=embed,
        view=attend_view,
        image_file=image_file,
        content=_boss_alert_content(boss_name, boss_time),
    )
    save_active_message(int(message_id), channel.id, boss_name, boss_time,
                        disable_time.strftime("%Y-%m-%d %H:%M:%S"),
                        created_date=datetime.now().strftime("%Y-%m-%d"),
                        attendees=attend_view.attendees,
                        loot=attend_view.loot,
                        loot_announcement_id=attend_view.loot_announcement_id,
                        image_url=attend_view.image_url,
                        image_filename=attend_view.image_filename)

    if minutes > 0:
        async def auto_disable():
            delay = (disable_time - datetime.now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await attend_view.disable_buttons(msg)
                log.info(f"[자동 마감] {boss_name} {boss_time} | 참석: {len(attend_view.attendees)}명")
            except Exception:
                pass
            remove_active_message(int(message_id))

        asyncio.ensure_future(auto_disable())

    deadline_msg = f"{minutes}분 후 마감" if minutes > 0 else "수동 마감 전까지 유지"
    log.info(f"[수동 활성화] {boss_name} {boss_time} | {deadline_msg} | 메시지 ID: {message_id}")
    await ctx.send(f"✅ **{boss_name}** {boss_time} 활성화! ({deadline_msg})")


@bot.hybrid_command()
async def extend(ctx, message_id: str, minutes: int = 20):
    """활성 메시지의 마감 시간 연장. 예: /extend 메시지ID 30"""
    active = load_active_messages()
    info = active.get(message_id)
    if not info:
        await ctx.send(f"❌ 활성 메시지가 아니에요! (ID: {message_id})")
        return

    new_disable_time = datetime.now() + timedelta(minutes=minutes)
    info["disable_time"] = new_disable_time.strftime("%Y-%m-%d %H:%M:%S")
    upsert_active_message_info(message_id, info)

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    try:
        msg = await channel.fetch_message(int(message_id))
        attend_view = AttendView(
            message_id=int(message_id),
            boss_name=info["boss_name"],
            boss_time=info["boss_time"],
            created_date=info.get("created_date"),
            loot=info.get("loot", ""),
            disable_time=new_disable_time,
        )
        attend_view.attendees = list(info.get("attendees", []))
        attend_view.loot_acquirers = info.get("loot_acquirers", "") or load_loot_acquirers_from_excel(int(message_id)) or ""
        attend_view.loot_announcement_id = (
            info.get("loot_announcement_id", "")
            or info.get("announcement_msg_id", "")
            or ""
        )
        attend_view.image_url = info.get("image_url", "")
        attend_view.image_filename = info.get("image_filename", "")
        image_filename, image_url = _message_image_ref(msg)
        if image_filename:
            attend_view.image_filename = image_filename
        if image_url:
            attend_view.image_url = image_url
        if attend_view.image_filename:
            info["image_filename"] = attend_view.image_filename
        if attend_view.image_url:
            info["image_url"] = attend_view.image_url
        if attend_view.loot_announcement_id:
            info["loot_announcement_id"] = attend_view.loot_announcement_id
        upsert_active_message_info(message_id, info)
        # 새 마감 시각이 적용된 embed 로 갱신 (카운트다운이 즉시 갱신됨)
        names = ", ".join(attend_view.attendees) if attend_view.attendees else "없음"
        try:
            image_file, image_kwargs = _boss_image_file_and_kwargs(attend_view.boss_name, msg)
            new_embed = make_attend_embed(
                attend_view.boss_name, len(attend_view.attendees), names, attend_view.boss_time,
                loot=attend_view.loot, disable_time=new_disable_time,
                loot_acquirers=getattr(attend_view, "loot_acquirers", ""),
                **image_kwargs,
            )
            await _edit_message_with_image(
                msg,
                embed=new_embed,
                view=attend_view,
                image_file=image_file,
                content=_boss_alert_content(attend_view.boss_name, attend_view.boss_time),
            )
        except Exception:
            pass

        async def auto_disable():
            delay = (new_disable_time - datetime.now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await attend_view.disable_buttons(msg)
                log.info(f"[자동 마감] {info['boss_name']} {info['boss_time']} | 참석: {len(attend_view.attendees)}명")
            except Exception:
                pass
            remove_active_message(int(message_id))

        asyncio.ensure_future(auto_disable())
    except Exception:
        pass

    log.info(f"[마감 연장] {info['boss_name']} {info['boss_time']} | +{minutes}분 | 메시지 ID: {message_id}")
    await ctx.send(f"✅ **{info['boss_name']}** {info['boss_time']} 마감 시간을 **{minutes}분** 연장했어요! ({new_disable_time.strftime('%H:%M')} 마감)")


@bot.hybrid_command()
async def delete(ctx, message_id: str):
    """알림 메시지를 삭제합니다. 예: /delete 메시지ID"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ 알림 채널을 찾을 수 없어요!")
        return

    try:
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        await ctx.send(f"❌ 메시지를 찾을 수 없어요! (ID: {message_id})")
        return

    try:
        await msg.delete()
        remove_active_message(int(message_id))
        log.info(f"[삭제] 메시지 ID: {message_id}")
        await ctx.send(f"✅ 메시지를 삭제했어요! (ID: {message_id})")
    except Exception as e:
        await ctx.send(f"❌ 삭제 실패: {e}")


@bot.hybrid_command()
async def test(ctx, boss_name: str = "테스트보스"):
    now = datetime.now()
    boss_time_str = (now + timedelta(minutes=1)).strftime("%H:%M")
    disable_time = now + timedelta(minutes=3)
    attend_view = AttendView(boss_name=boss_name, boss_time=boss_time_str, disable_time=disable_time)
    msg = await ctx.send(
        content=_boss_alert_content(boss_name, boss_time_str),
        embed=make_attend_embed(boss_name, 0, "없음", boss_time_str, disable_time=disable_time),
        view=attend_view,
    )
    attend_view.message_id = msg.id

    save_active_message(msg.id, ctx.channel.id, boss_name, boss_time_str, disable_time.strftime("%Y-%m-%d %H:%M:%S"), created_date=now.strftime("%Y-%m-%d"))

    async def auto_disable():
        await asyncio.sleep(120)
        try:
            await attend_view.disable_buttons(msg)
        except Exception:
            pass
        remove_active_message(msg.id)

    asyncio.ensure_future(auto_disable())


@bot.hybrid_command()
async def fixnames(ctx, channel_id: int = 0, limit: int = 200):
    """채널의 봇 메시지에서 사용자 ID를 이름으로 교체. 예: /fixnames / /fixnames 채널ID / /fixnames 채널ID 100"""
    id_to_name = _load_id_to_name_mapping()
    if not id_to_name:
        await ctx.send("❌ user_mapping.json 데이터가 없어요.")
        return

    uid_pattern = re.compile(r'\b(\d{17,20})\b')

    def replace_uids(text):
        return uid_pattern.sub(lambda m: id_to_name.get(m.group(1), m.group(1)), text)

    target_channel = bot.get_channel(channel_id) if channel_id else ctx.channel
    if not target_channel:
        try:
            target_channel = await bot.fetch_channel(channel_id)
        except Exception:
            await ctx.send(f"❌ 채널 {channel_id}을 찾을 수 없어요.")
            return

    fixed = 0
    skipped = 0
    async for message in target_channel.history(limit=limit):
        if message.author.id != bot.user.id:
            continue
        if not message.embeds:
            continue
        embed = message.embeds[0]
        desc = embed.description or ""
        if not uid_pattern.search(desc):
            continue
        new_desc = replace_uids(desc)
        if new_desc == desc:
            skipped += 1
            continue
        new_embed = discord.Embed(color=embed.color, description=new_desc)
        if embed.title:
            new_embed.title = embed.title
        if getattr(embed.thumbnail, "url", None):
            new_embed.set_thumbnail(url=embed.thumbnail.url)
        if getattr(embed.image, "url", None):
            new_embed.set_image(url=embed.image.url)
        for field in embed.fields:
            new_embed.add_field(name=field.name, value=replace_uids(field.value), inline=field.inline)
        try:
            await message.edit(embed=new_embed)
            fixed += 1
        except Exception as e:
            log.info(f"[fixnames] 메시지 {message.id} 수정 실패: {e}")

    await ctx.send(f"✅ {target_channel.mention} — {fixed}개 메시지 수정 완료 (UID 없는 메시지 {skipped}개 건너뜀)")


def _fmt_boss_time_cell(value):
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value or "").strip()


@bot.hybrid_command()
async def fixmsg(ctx, *, message_ids: str = ""):
    """특정 메시지 ID의 embed UID를 이름으로 교체. 예: /fixmsg 1506954706746871913 1506984905509441540"""
    message_ids = tuple(message_ids.split())
    if not message_ids:
        await ctx.send("❌ 메시지 ID를 입력해 주세요. 예: `/fixmsg 1234567890 9876543210`")
        return

    id_to_name = _load_id_to_name_mapping()
    if not id_to_name:
        await ctx.send("❌ user_mapping.json 데이터가 없어요.")
        return

    uid_pattern = re.compile(r'\b(\d{17,20})\b')

    def replace_uids(text):
        return uid_pattern.sub(lambda m: id_to_name.get(m.group(1), m.group(1)), text)

    # 검색할 채널 목록 (알림 채널 + 커맨드 채널 우선)
    search_channels = []
    for ch_id in {ALERT_CHANNEL_ID, COMMAND_CHANNEL_ID, LOOT_CHANNEL_ID}:
        if ch_id:
            ch = bot.get_channel(ch_id)
            if not ch:
                try:
                    ch = await bot.fetch_channel(ch_id)
                except Exception:
                    continue
            if ch:
                search_channels.append(ch)

    results = []
    for raw_id in message_ids:
        if not raw_id.isdigit():
            results.append(f"❌ `{raw_id}` — 숫자 ID가 아니에요.")
            continue
        msg = None
        for ch in search_channels:
            try:
                msg = await ch.fetch_message(int(raw_id))
                break
            except Exception:
                continue
        if not msg:
            results.append(f"❌ `{raw_id}` — 메시지를 찾을 수 없어요.")
            continue
        if not msg.embeds:
            results.append(f"⚠️ `{raw_id}` — embed가 없는 메시지예요.")
            continue
        embed = msg.embeds[0]
        desc = embed.description or ""
        new_desc = replace_uids(desc)
        if new_desc == desc:
            results.append(f"✅ `{raw_id}` — 수정할 UID가 없어요.")
            continue
        new_embed = discord.Embed(color=embed.color, description=new_desc)
        if embed.title:
            new_embed.title = embed.title
        if getattr(embed.thumbnail, "url", None):
            new_embed.set_thumbnail(url=embed.thumbnail.url)
        if getattr(embed.image, "url", None):
            new_embed.set_image(url=embed.image.url)
        for field in embed.fields:
            new_embed.add_field(name=field.name, value=replace_uids(field.value), inline=field.inline)
        try:
            await msg.edit(embed=new_embed)
            results.append(f"✅ `{raw_id}` — 수정 완료")
        except Exception as e:
            results.append(f"❌ `{raw_id}` — 수정 실패: {e}")

    await ctx.send("\n".join(results))


def verify_and_fix_from_logs(days=7):
    """로그 파일에서 참석/취소 이벤트를 파싱해 Excel 누락 참석자만 보강한다.

    Excel은 마감 시점의 확정 저장본에 가깝고, 로그는 회전/누락 가능성이 있으므로
    로그가 Excel보다 적은 경우에는 기존 Excel 데이터를 자동 축소하지 않는다.
    """
    # 1. 로그 파일 수집
    log_files = []
    for i in range(days):
        date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        log_path = os.path.join(LOG_DIR, f"RaidBook_Boss_Bot_{date_str}.log")
        if os.path.exists(log_path):
            log_files.append(log_path)

    if not log_files:
        return 0

    # 2. 로그 파싱 — 메시지 ID 매핑 + 참석/취소 추적
    # (boss_name, boss_time) → message_id
    alert_map = {}
    # message_id → 참석자 순서 리스트 [(action, name), ...]
    event_log = defaultdict(list)

    re_alert = re.compile(r'\[알림 전송\] (.+?) (.+?) \| 메시지 ID: (\d+)')
    re_create = re.compile(r'\[수동 생성\] (.+?) (.+?) \| .+ \| 메시지 ID: (\d+)')
    # 새 형식: [참석] 20260516 카스파 17:00 1505112567712972881 | 명가_짱어 (총 1명)
    # (UID 병기 형식 "이름 [UID]"도 지원 — 이름 비교 기반 복구이므로 UID 부분은 분리해서 제외)
    re_join_new = re.compile(r'\[참석\] \d{8} (.+?) (.+?) (\d+) \| (.+?)(?: \[\d+\])? \(총 \d+명\)')
    re_cancel_new = re.compile(r'\[취소\] \d{8} (.+?) (.+?) (\d+) \| (.+?)(?: \[\d+\])? \(총 \d+명\)')
    # 구 형식 호환: [참석] 카스파 17:00 | 명가_짱어 (총 1명)
    re_join_old = re.compile(r'\[참석\] (.+?) (.+?) \| (.+?) \(총 \d+명\)')
    re_cancel_old = re.compile(r'\[취소\] (.+?) (.+?) \| (.+?) \(총 \d+명\)')

    for log_path in log_files:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                m = re_alert.search(line)
                if m:
                    boss_name, boss_time, msg_id = m.group(1), m.group(2), m.group(3)
                    alert_map[(boss_name, boss_time)] = msg_id
                    continue

                m = re_create.search(line)
                if m:
                    boss_name, boss_time, msg_id = m.group(1), m.group(2), m.group(3)
                    alert_map[(boss_name, boss_time)] = msg_id
                    continue

                # 새 형식 (메시지 ID 포함)
                m = re_join_new.search(line)
                if m:
                    msg_id, name = m.group(3), m.group(4)
                    event_log[msg_id].append(("join", name))
                    continue

                m = re_cancel_new.search(line)
                if m:
                    msg_id, name = m.group(3), m.group(4)
                    event_log[msg_id].append(("cancel", name))
                    continue

                # 구 형식 (alert_map으로 매칭)
                m = re_join_old.search(line)
                if m:
                    boss_name, boss_time, name = m.group(1), m.group(2), m.group(3)
                    msg_id = alert_map.get((boss_name, boss_time))
                    if msg_id:
                        event_log[msg_id].append(("join", name))
                    continue

                m = re_cancel_old.search(line)
                if m:
                    boss_name, boss_time, name = m.group(1), m.group(2), m.group(3)
                    msg_id = alert_map.get((boss_name, boss_time))
                    if msg_id:
                        event_log[msg_id].append(("cancel", name))

    # 3. 이벤트 리플레이 — 최종 참석자 리스트 구성 (이름 → UID 변환)
    n2u = _name_to_uid_mapping()
    log_attendees = {}
    for msg_id, events in event_log.items():
        attendees_names = []
        for action, name in events:
            if action == "join" and name not in attendees_names:
                attendees_names.append(name)
            elif action == "cancel" and name in attendees_names:
                attendees_names.remove(name)
        # 이름 → UID 변환
        log_attendees[msg_id] = [n2u.get(n, n) for n in attendees_names]

    # 4. Excel 데이터와 비교 + 보정
    if not os.path.exists(EXCEL_PATH):
        return 0

    wb = load_workbook(EXCEL_PATH)
    planned_fixes = {}

    for msg_id, log_att in log_attendees.items():
        # Excel에서 해당 메시지의 참석자 조회 (col4 = UID)
        excel_att = []
        for sheet_name in wb.sheetnames:
            if sheet_name in SYSTEM_SHEETS:
                continue
            ws = wb[sheet_name]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[0] and str(row[0]) == msg_id and row[3]:
                    excel_att.append(str(row[3]))

        log_set = {str(x) for x in log_att if x}
        excel_set = {str(x) for x in excel_att if x}
        missing_from_excel = [uid for uid in log_att if uid and str(uid) not in excel_set]
        missing_from_log = [uid for uid in excel_att if uid and str(uid) not in log_set]

        if missing_from_excel:
            merged = list(excel_att)
            for uid in missing_from_excel:
                if uid not in merged:
                    merged.append(uid)
            planned_fixes[msg_id] = merged
            log.info(
                f"[로그 검증] 메시지 {msg_id} | Excel 누락 참석자 {missing_from_excel} 추가 예정 "
                f"(Excel {len(excel_set)}명 → 병합 {len(set(merged))}명)"
            )
        elif missing_from_log:
            log.info(
                f"[로그 검증 보류] 메시지 {msg_id} | 로그에 없는 Excel 참석자 {missing_from_log} 유지 "
                f"(로그 {len(log_set)}명, Excel {len(excel_set)}명)"
            )

    wb.close()

    fixed = 0

    # 실제 보정: 로그에만 있는 누락 참석자가 있는 메시지에 대해서만 Excel 업데이트
    for msg_id, merged_att in planned_fixes.items():
        excel_att = []
        if os.path.exists(EXCEL_PATH):
            wb_check = load_workbook(EXCEL_PATH)
            for sheet_name in wb_check.sheetnames:
                if sheet_name in SYSTEM_SHEETS:
                    continue
                ws = wb_check[sheet_name]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row[0] and str(row[0]) == msg_id and row[3]:
                        excel_att.append(row[3])
            wb_check.close()

        excel_set = {str(x) for x in excel_att if x}
        still_missing = [uid for uid in merged_att if uid and str(uid) not in excel_set]
        if still_missing:
            # alert_map에서 boss_name, boss_time 찾기
            boss_name_found = ""
            boss_time_found = ""
            for (bn, bt), mid in alert_map.items():
                if mid == msg_id:
                    boss_name_found = bn
                    boss_time_found = bt
                    break

            if boss_name_found:
                # 메시지 생성 날짜 추출
                discord_epoch = 1420070400000
                timestamp_ms = (int(msg_id) >> 22) + discord_epoch
                created_date = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")

                # 기존 Excel row는 유지하고, 로그에만 있는 누락 참석자만 추가한다.
                _sync_save_to_excel(msg_id, boss_name_found, boss_time_found, still_missing, created_date)
                fixed += 1

    return fixed


def _sync_save_to_excel(message_id, boss_name, boss_time, attendees, created_date):
    """동기적으로 Excel에 참석자 저장 (로그 검증용). attendees는 이름 또는 UID 리스트."""
    today = created_date
    if not os.path.exists(EXCEL_PATH):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = today
        apply_header(ws, RAW_HEADERS)
    else:
        wb = load_workbook(EXCEL_PATH)

    if today not in wb.sheetnames:
        ws = wb.create_sheet(today)
        apply_header(ws, RAW_HEADERS)
    else:
        ws = wb[today]

    str_message_id = str(message_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n2u = _name_to_uid_mapping()
    id2n = _load_id_to_name_mapping()

    if not attendees:
        next_row = ws.max_row + 1
        ws.cell(row=next_row, column=1, value=str_message_id).number_format = '@'
        ws.cell(row=next_row, column=2, value=boss_name)
        ws.cell(row=next_row, column=3, value=boss_time)
        ws.cell(row=next_row, column=4, value="")
        ws.cell(row=next_row, column=5, value=now_str)
    else:
        for attendee in attendees:
            # 이름이면 UID로 변환, 이미 UID면 그대로
            uid = n2u.get(str(attendee), str(attendee)) if not str(attendee).isdigit() else str(attendee)
            display = id2n.get(uid, str(attendee))
            next_row = ws.max_row + 1
            ws.cell(row=next_row, column=1, value=str_message_id).number_format = '@'
            ws.cell(row=next_row, column=2, value=boss_name)
            ws.cell(row=next_row, column=3, value=boss_time)
            ws.cell(row=next_row, column=4, value=uid)      # col4 = UID
            ws.cell(row=next_row, column=5, value=now_str)
            ws.cell(row=next_row, column=6, value=display)  # col6 = 이름
            ws.cell(row=next_row, column=7, value=calculate_points(boss_name, boss_time))

    save_ok, save_err = save_workbook_safely_sync(wb, EXCEL_PATH, "보스 데이터")
    wb.close()
    if save_ok:
        backup_excel()
    else:
        log.info(f"[동기 저장 실패] {save_err}")


async def recover_missing_from_channel():
    """Discord 채널에서 누락된 데이터를 자동 복구"""
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return 0

    # Excel에 있는 메시지 ID 수집
    existing_ids = set()
    if os.path.exists(EXCEL_PATH):
        wb = load_workbook(EXCEL_PATH)
        for sheet_name in wb.sheetnames:
            if sheet_name in SYSTEM_SHEETS:
                continue
            ws = wb[sheet_name]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[0]:
                    existing_ids.add(str(row[0]))

    # 최근 7일 메시지 스캔
    recovered = 0
    cutoff = datetime.now() - timedelta(days=7)
    discord_epoch = 1420070400000

    async for msg in channel.history(limit=500, after=cutoff):
        if msg.author.id != bot.user.id:
            continue
        if not msg.embeds:
            continue

        msg_id_str = str(msg.id)
        if msg_id_str in existing_ids:
            continue

        embed = msg.embeds[0]
        desc = embed.description or ""
        title = embed.title or ""
        if "보스 출현 알림" not in f"{title}\n{desc}":
            continue
        if "마감됨" not in desc:
            continue

        # embed 파싱
        boss_name = ""
        boss_time = ""
        attendees = []

        if "보스 출현 알림" in title:
            boss_name = title.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
        for line in desc.split("\n"):
            if "보스 출현 알림" in line:
                boss_name = line.replace("📢", "").replace("**", "").replace("보스 출현 알림", "").strip()
            elif "보스 리젠 시각" in line:
                boss_time = line.split("**")[-2].strip() if "**" in line else ""
            elif "참석:" in line:
                parts = line.split("|")
                if len(parts) > 1:
                    names_str = parts[-1].strip()
                    if names_str and names_str != "없음":
                        attendees = [n.strip() for n in names_str.split(",") if n.strip()]

        if not boss_name:
            continue

        timestamp_ms = (int(msg_id_str) >> 22) + discord_epoch
        created_date = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")

        await save_boss_attendees_to_excel(int(msg_id_str), boss_name, boss_time or "알 수 없음", attendees, created_date)
        recovered += 1
        log.info(f"[자동 복구] {boss_name} {boss_time} | {created_date} | 참석: {len(attendees)}명")

    return recovered


async def daily_refresh():
    """매일 지정 시각에 누락 복구 + 데이터 정리"""
    while True:
        now = datetime.now()
        target = now.replace(hour=daily_refresh_hour, minute=daily_refresh_minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)

        # 1단계: 누락 데이터 자동 복구 (Discord 채널 스캔)
        try:
            recovered = await recover_missing_from_channel()
            if recovered > 0:
                log.info(f"[일일 복구] {recovered}건 누락 데이터 복구 완료")
        except Exception as e:
            log.info(f"[일일 복구 오류] {e}")

        # 2단계: 로그 기반 누락 참석자 보강 (Excel 축소 금지)
        verified = 0
        try:
            verified = verify_and_fix_from_logs()
            if verified > 0:
                log.info(f"[로그 검증] {verified}건 데이터 보정 완료")
        except Exception as e:
            log.info(f"[로그 검증 오류] {e}")

        if not os.path.exists(EXCEL_PATH):
            continue

        guild_updated = await update_mapping_from_guild(bot.guilds[0] if bot.guilds else None)
        message_map = await build_message_map_from_channel()
        async with excel_lock:
            try:
                wb = load_workbook(EXCEL_PATH)
                auto_migrate_excel(wb)
                fix_missing_ids(wb, message_map)
                for sheet_name in wb.sheetnames:
                    if sheet_name not in SYSTEM_SHEETS:
                        reapply_raw_sheet_format(wb[sheet_name])
                mapped_count = apply_user_id_mapping(wb)
                update_history_sheet(wb)
                update_boss_attendance_sheet(wb)
                update_stats_sheet(wb)
                reorder_sheets(wb)
                save_ok, save_err = await save_workbook_safely(wb, EXCEL_PATH, "보스 데이터")
                if not save_ok:
                    log.info(f"[일일정리 저장 실패] {save_err}")
                    continue
                backup_excel()
                backup_excel_daily_snapshot()  # 자정 일자별 스냅샷 (최대 30일 보관)
                log.info(f"[일일정리] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 완료")

                status_parts = []
                if guild_updated > 0:
                    status_parts.append(f"서버 멤버 {guild_updated}명 매핑 갱신")
                if recovered > 0:
                    status_parts.append(f"누락 {recovered}건 복구")
                if verified > 0:
                    status_parts.append(f"로그 검증 {verified}건 보정")
                if mapped_count > 0:
                    status_parts.append(f"유저 ID {mapped_count}건 매핑")
                status_msg = f" ({', '.join(status_parts)})" if status_parts else ""
                channel = bot.get_channel(COMMAND_CHANNEL_ID)
                if channel:
                    await channel.send(
                        f"🔄 **일일 데이터 정리 완료!{status_msg}** `{datetime.now().strftime('%Y-%m-%d %H:%M')}`",
                        file=discord.File(EXCEL_PATH)
                    )
            except Exception as e:
                log.info(f"[일일정리 오류] {e}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if ctx.interaction is not None:
        # 슬래시 명령은 오류도 응답하지 않으면 '응답하지 않음'으로 남으므로 사용자에게 알림
        if isinstance(error, commands.CheckFailure):
            text = f"⛔ {error}"
        elif isinstance(error, (commands.UserInputError, commands.BadArgument)):
            text = f"❌ 입력값을 확인해 주세요: {error}"
        else:
            text = "❌ 명령 처리 중 오류가 발생했어요."
            log.warning(f"[슬래시 명령 오류] /{ctx.command and ctx.command.name}: {type(error).__name__}: {error}")
        try:
            await ctx.send(text, ephemeral=True)
        except Exception:
            pass
        return
    raise error


@bot.event
async def on_ready():
    global on_ready_initialized, on_ready_initializing
    if on_ready_initialized or on_ready_initializing:
        log.info("[on_ready] 재호출 감지 — 초기화 생략")
        return
    on_ready_initializing = True
    try:
        await initialize_on_ready()
        on_ready_initialized = True
    finally:
        on_ready_initializing = False


async def _sync_slash_commands():
    """슬래시(/) 명령을 봇이 속한 각 서버에 즉시 동기화 (전역 동기화는 반영까지 최대 1시간)."""
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"[슬래시 동기화] {guild.name}({guild.id}) {len(synced)}개")
        except Exception as e:
            log.warning(f"[슬래시 동기화 실패] {guild.name}({guild.id}): {type(e).__name__}: {e}")


async def _ensure_boss_channels():
    """없는 채널 자동 생성 후 채널 전역값을 갱신. 결과 안내 문자열 목록 반환."""
    global ALERT_CHANNEL_ID, COMMAND_CHANNEL_ID, LOOT_CHANNEL_ID, AUCTION_CHANNEL_ID, VOICE_CHANNEL_IDS
    from RaidBook_channel_setup import ChannelSpec, ensure_channels, resolve_guild
    keys = ["alert_channel_id", "command_channel_id", "loot_channel_id", "auction_channel_id", "voice_channel_ids"]
    guild = resolve_guild(bot, config, keys)
    specs = [
        ChannelSpec("alert_channel_id", "보탐-알림"),
        ChannelSpec("command_channel_id", "보탐-명령"),
    ]
    if LOOT_ANNOUNCE:
        specs.append(ChannelSpec("loot_channel_id", "전리품"))
    if LOOT_AUCTION_BUTTON:
        specs.append(ChannelSpec("auction_channel_id", "경매장"))
    # 음성채널 목록이 비어 있으면 '서버 전체 음성채널' 모드 — 음성채널이 하나도 없을 때만 만든다
    if config.get("voice_channel_ids") or (guild is not None and not guild.voice_channels):
        specs.append(ChannelSpec("voice_channel_ids", "보탐-음성", voice=True, is_list=True))
    try:
        created, problems = await ensure_channels(bot, CONFIG_PATH, config, specs, log, guild=guild)
    except Exception as e:
        log.warning(f"[채널 자동 생성] 오류: {type(e).__name__}: {e}")
        return [f"⚠️ 채널 자동 생성 오류: {e}"]
    ALERT_CHANNEL_ID = config.get("alert_channel_id")
    COMMAND_CHANNEL_ID = config.get("command_channel_id") or ALERT_CHANNEL_ID
    LOOT_CHANNEL_ID = config.get("loot_channel_id") or COMMAND_CHANNEL_ID
    AUCTION_CHANNEL_ID = int(config.get("auction_channel_id") or 0)
    VOICE_CHANNEL_IDS = [int(c) for c in (config.get("voice_channel_ids") or []) if c]
    boss_auction.channel_id = AUCTION_CHANNEL_ID
    notes = [f"🆕 <#{ids[0]}> 채널을 만들었어요 (`{key}`)" for key, ids in created.items()]
    for p in problems:
        log.warning(f"[채널 자동 생성] {p}")
        notes.append(f"⚠️ {p}")
    return notes


async def initialize_on_ready():
    global daily_refresh_task
    channel_notes = await _ensure_boss_channels() if AUTO_CREATE_CHANNELS else []
    _auto_migrate_user_mapping()
    bot.add_view(AttendView())
    bot.add_view(LootAuctionView())  # 전리품 공지 경매시작 버튼 재시작 복구
    bot.add_view(AuctionView(boss_auction))  # 경매 메시지 버튼 재시작 복구
    bot.add_view(BoardView(boss_auction))  # 경매 진행판 버튼 재시작 복구
    await boss_auction.restore()
    global _close_sweep_task
    if _close_sweep_task is None or _close_sweep_task.done():
        _close_sweep_task = asyncio.ensure_future(_close_sweep_loop())
    await _sync_slash_commands()
    now = datetime.now()
    channel = bot.get_channel(COMMAND_CHANNEL_ID)
    if channel:
        await channel.send("\n".join([f"🟢 **보스 알림 봇이 시작되었습니다!** `{now.strftime('%Y-%m-%d %H:%M')}`"] + channel_notes))
    log.info(f"{bot.user} 준비 완료!")

    # 활성 메시지 auto_disable 복구
    cmd_channel = bot.get_channel(COMMAND_CHANNEL_ID)
    active_messages = load_active_messages()
    restore_logs = []

    for msg_id_str, info in list(active_messages.items()):
        # disable_time이 빈 문자열/없음 → 마감 비활성화 (무기한 유지)
        dt_str = info.get("disable_time") or ""
        if dt_str:
            try:
                disable_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                disable_time = None
        else:
            disable_time = None
        remaining = (disable_time - now).total_seconds() if disable_time else None
        ch = bot.get_channel(info["channel_id"])
        if not ch:
            remove_active_message(msg_id_str)
            restore_logs.append(f"  ❌ **{info.get('boss_name', '?')}** {info.get('boss_time', '?')} — 채널 없음, 제거")
            continue

        try:
            msg = await ch.fetch_message(int(msg_id_str))
        except Exception:
            remove_active_message(msg_id_str)
            restore_logs.append(f"  ❌ **{info.get('boss_name', '?')}** {info.get('boss_time', '?')} — 메시지 없음, 제거")
            continue

        attend_view = AttendView(
            message_id=int(msg_id_str),
            boss_name=info["boss_name"],
            boss_time=info["boss_time"],
            created_date=info.get("created_date"),
            loot=info.get("loot", ""),
            disable_time=disable_time,
        )
        attend_view.attendees = list(info.get("attendees", []))
        attend_view.loot_acquirers = info.get("loot_acquirers", "") or load_loot_acquirers_from_excel(int(msg_id_str)) or ""
        attend_view.loot_announcement_id = (
            info.get("loot_announcement_id", "")
            or info.get("announcement_msg_id", "")
            or ""
        )
        attend_view.image_url = info.get("image_url", "")
        attend_view.image_filename = info.get("image_filename", "")
        image_filename, image_url = _message_image_ref(msg)
        if image_filename:
            attend_view.image_filename = image_filename
            info["image_filename"] = image_filename
        if image_url:
            attend_view.image_url = image_url
            info["image_url"] = image_url
        upsert_active_message_info(msg_id_str, info)
        attendee_count = len(attend_view.attendees)

        # 파티 미확정 상태면 확정 타이머 재등록
        if not info.get("party_confirmed", False):
            _schedule_party_confirm_task(attend_view, int(msg_id_str), info["channel_id"], info["boss_time"])
        # 출현 전이면 정각 인원체크 재예약
        try:
            _spawn_dt = datetime.strptime(f"{info.get('created_date')} {info['boss_time']}", "%Y-%m-%d %H:%M")
        except Exception:
            _spawn_dt = None
        if remaining is None or remaining > 0:
            _schedule_voice_check(int(msg_id_str), _spawn_dt)

        if remaining is None:
            # 마감 비활성화 — 무기한 유지, auto_disable 타이머 등록 안 함
            restore_logs.append(f"  ♾️ **{info['boss_name']}** {info['boss_time']} — 무기한 활성 (참석 {attendee_count}명)")
            log.info(f"[복구] 메시지 {msg_id_str} 무기한 활성 유지 (close_minutes 비활성)")
        elif remaining <= 0:
            try:
                await attend_view.disable_buttons(msg)
            except Exception as e:
                # 하나가 실패해도 나머지 복구는 계속 — 남은 건은 마감 점검 루프가 다시 시도
                log.info(f"[복구] 메시지 {msg_id_str} 즉시 마감 실패: {type(e).__name__}: {e}")
                restore_logs.append(f"  ⚠️ **{info['boss_name']}** {info['boss_time']} — 마감 실패, 1분 내 재시도")
                continue
            remove_active_message(msg_id_str)
            restore_logs.append(f"  🔒 **{info['boss_name']}** {info['boss_time']} — 즉시 마감 (참석 {attendee_count}명)")
            log.info(f"[복구] 메시지 {msg_id_str} 즉시 마감")
        else:
            async def auto_disable_restore(v=attend_view, m=msg, mid=msg_id_str, d=remaining):
                await asyncio.sleep(d)
                try:
                    await v.disable_buttons(m)
                except Exception as e:
                    log.info(f"[복구 마감 실패] {mid}: {type(e).__name__}: {e} — 점검 루프가 재시도")
                    return
                remove_active_message(mid)

            asyncio.ensure_future(auto_disable_restore())
            restore_logs.append(f"  🔄 **{info['boss_name']}** {info['boss_time']} — {int(remaining)}초 후 마감 (참석 {attendee_count}명)")
            log.info(f"[복구] 메시지 {msg_id_str} auto_disable 재등록 ({int(remaining)}초 후)")

    if cmd_channel and restore_logs:
        restore_text = "📋 **활성 메시지 복구 결과**\n" + "\n".join(restore_logs)
        await cmd_channel.send(restore_text)
    elif cmd_channel and active_messages:
        await cmd_channel.send("📋 **복구할 활성 메시지가 없습니다.**")

    # 재시작 시 레지스트리의 이전 boss 항목 초기화 (아래에서 실제 시작분만 다시 게시)
    rb_schedule.clear_bot_schedules(_SCHED_REGISTRY_PATH, "boss", log)

    for boss_name, boss_cfg in BOSSES_CONFIG.items():
        interval = boss_cfg.get("interval_hours", 2)
        start_hour = boss_cfg.get("start_hour", 17)
        start_minute = boss_cfg.get("start_minute", 0)

        first_boss = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        first_alert = first_boss - timedelta(minutes=DEFAULT_ALERT_MINUTES)

        while first_alert < now:
            first_boss += timedelta(hours=interval)
            first_alert = first_boss - timedelta(minutes=DEFAULT_ALERT_MINUTES)

        log.info(f"[{boss_name}] 첫 참석 알림: {first_alert.strftime('%H:%M')}")
        log.info(f"[{boss_name}] 첫 보스 알림: {first_boss.strftime('%H:%M')}")

        task = asyncio.ensure_future(schedule_boss(boss_name, first_alert, interval))
        boss_states[boss_name] = {"task": task, "interval_hours": interval}
        _publish_boss_schedule(boss_name, {"type": "interval", "interval_hours": interval,
                                           "start_hour": start_hour, "start_minute": start_minute})

    # daily_bosses config 자동 시작
    for boss_name, boss_cfg in DAILY_BOSSES_CONFIG.items():
        times = boss_cfg.get("times", [])
        alert_min = boss_cfg.get("alert_minutes", DEFAULT_ALERT_MINUTES)
        if not times:
            continue
        boss_daily_states[boss_name] = {}
        for t in times:
            task = asyncio.ensure_future(schedule_boss_fixed_time(boss_name, t, alert_min))
            boss_daily_states[boss_name][t] = task
        _publish_boss_schedule(boss_name, {"type": "daily", "times": list(times)})
        log.info(f"[daily] {boss_name} 시작: {times} | {alert_min}분 전 알림")

    daily_refresh_task = asyncio.ensure_future(daily_refresh())
    await start_ipc_server()


# === IPC 서버 (Watchdog 연동) ===
IPC_PORT = config.get("ipc_port", 0)


class FakeCtx:
    class _Author:
        display_name = "Watchdog"
        id = 0
        mention = "@Watchdog"

    class _Message:
        id = 0

    def __init__(self):
        self.messages = []
        self.author = self._Author()
        self.message = self._Message()
        self.guild = None
        self.channel = None
        self.tx_status = None
        self.bot = bot

    async def send(self, content=None, embed=None, **kwargs):
        if content is not None:
            self.messages.append(str(content))
        if embed is not None:
            parts = []
            if getattr(embed, "title", None):
                parts.append(f"**{embed.title}**")
            if getattr(embed, "description", None):
                parts.append(embed.description)
            for field in getattr(embed, "fields", []):
                parts.append(f"**{field.name}**: {field.value}")
            if parts:
                self.messages.append("\n".join(parts))
        return type("FakeMsg", (), {"id": 0})()

    async def reply(self, content=None, embed=None, **kwargs):
        return await self.send(content=content, embed=embed)


async def _ipc_handle(reader, writer):
    resp = {"ok": False, "error": "알 수 없는 오류"}
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        req = json.loads(line.decode("utf-8"))
        cmd_name = req.get("cmd", "")
        args = req.get("args", [])
        cmd = bot.get_command(cmd_name)
        if cmd is None:
            resp = {"ok": False, "error": f"명령어 없음: {cmd_name}"}
        else:
            ctx = FakeCtx()
            try:
                await cmd.callback(ctx, *args)
                resp = {"ok": True, "messages": ctx.messages}
            except Exception as e:
                resp = {"ok": False, "error": str(e)}
    except Exception as e:
        resp = {"ok": False, "error": f"IPC 오류: {e}"}
    await write_json_response(writer, resp)


async def start_ipc_server():
    if not IPC_PORT:
        return
    server = await asyncio.start_server(_ipc_handle, "127.0.0.1", IPC_PORT)
    log.info(f"[IPC] 서버 시작: 127.0.0.1:{IPC_PORT}")
    asyncio.ensure_future(server.serve_forever())


async def _active_boss_autocomplete(interaction: discord.Interaction, current: str):
    """/인원체크 보스 옵션 드롭다운 — 마감 전 보스 알림 목록 (값=메시지ID)."""
    items = sorted(load_active_messages().items(),
                   key=lambda kv: (kv[1].get("created_date", ""), kv[1].get("boss_time", "")))
    choices = []
    for mid, info in items:
        label = f"{info.get('boss_name', '?')} {info.get('boss_time', '')} ({info.get('created_date', '')})"
        if current and current not in label and current not in mid:
            continue
        choices.append(discord.app_commands.Choice(name=label[:100], value=str(mid)))
    return choices[:25]


@bot.hybrid_command(name="인원체크", aliases=["voicecheck"])
@discord.app_commands.rename(message_id="보스")
@discord.app_commands.describe(message_id="인원을 넣을 보스 알림 (비우면 마감 전 알림 전체)")
@discord.app_commands.autocomplete(message_id=_active_boss_autocomplete)
async def voicecheck(ctx, message_id: str = None):
    """명령을 입력한 음성채널의 접속자를 진행 중인 보스 알림에 참석 처리. 예: /인원체크 / /인원체크 보스:카스파"""
    # 음성채널 채팅에서 입력 → 그 음성채널, 아니면 입력자가 접속 중인 음성채널
    vc = ctx.channel if isinstance(ctx.channel, (discord.VoiceChannel, discord.StageChannel)) else None
    if vc is None and isinstance(ctx.author, discord.Member) and ctx.author.voice:
        vc = ctx.author.voice.channel
    if vc is None:
        await ctx.send("❌ 음성채널 채팅에서 입력하거나, 음성채널에 접속한 상태로 사용해 주세요.")
        return
    targets = [message_id] if message_id else list(load_active_messages().keys())
    if not targets:
        await ctx.send("진행 중인 보스 알림이 없어요.")
        return
    lines = []
    for mid in targets:
        info = load_active_messages().get(str(mid)) or {}
        result = await voice_check_message(mid, vc)
        if result is None:
            lines.append(f"❌ `{mid}` — 진행 중인 알림이 아니에요.")
        else:
            lines.append(f"🎙 **{info.get('boss_name', '?')}** {info.get('boss_time', '')} — "
                         f"{result[0]}명 추가 (총 {result[1]}명)")
    await ctx.send((f"🎙 <#{vc.id}> 기준\n" + "\n".join(lines))[:1990])


@bot.hybrid_command()
async def collectmembers(ctx):
    """서버 전체 멤버의 uid → DisplayName을 user_mapping.json에 저장.
    예: /collectmembers"""
    guild = ctx.guild
    if not guild:
        await ctx.send("❌ 서버에서 실행해주세요.")
        return

    status = await ctx.send("⏳ 멤버 목록 수집 중...")
    try:
        # members intent 필요 — 전체 멤버 fetch
        members = guild.members if guild.member_count and len(guild.members) >= guild.member_count * 0.9 \
                  else await guild.fetch_members(limit=None).flatten()
    except Exception:
        members = guild.members

    mapping = _load_id_to_name_mapping()
    added = 0
    updated = 0
    for member in members:
        if member.bot:
            continue
        uid = str(member.id)
        name = member.display_name
        if uid not in mapping:
            added += 1
        elif mapping[uid] != name:
            updated += 1
        mapping[uid] = name

    write_json_atomic(USER_MAPPING_PATH, mapping, log)

    log.info(f"[멤버 수집] 서버: {guild.name} | 총 {len(members)}명 | 신규: {added} | 업데이트: {updated}")
    await status.edit(content=f"✅ 멤버 수집 완료!\n총 **{len(members)}명** | 신규 등록: **{added}명** | 이름 갱신: **{updated}명**")


if __name__ == "__main__":
    # import만으로 봇이 실행되지 않도록 가드 (도구/테스트에서의 중복 로그인 방지)
    bot.run(TOKEN)
