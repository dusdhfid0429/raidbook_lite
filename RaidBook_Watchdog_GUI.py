# -*- coding: utf-8 -*-
"""
RaidBook Watchdog GUI - 봇 프로세스 감시 및 자동 재시작 (GUI 버전)
사용법: python RaidBook_Watchdog_GUI.py
config: Config/watchdog_config.json

tkinter 기반 GUI — 봇 상태 실시간 표시, 버튼으로 시작/중지/재시작,
로그 뷰어, 네트워크 상태 패널 제공.

Discord 명령 봇 / 프로세스 감시 / IPC 포워딩 / 정산-설문-참석통계 로직은
모두 RaidBook_Watchdog.py 가 단일 소스로 소유한다. 이 파일은 그 위에
tkinter 트레이 아이콘, Windows 토스트 알림, 초기 설정 마법사, 단일
인스턴스 잠금 같은 GUI 전용 기능만 얹는다.
"""
import subprocess
import time
import os
import sys
import json
import logging
import threading
import asyncio
import queue
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, font as tkfont

# ── 공유 코어: Discord 봇 / 프로세스 감시 / IPC / 정산·설문·참석통계 ────────
import RaidBook_Watchdog as _core
from RaidBook_Watchdog import (
    log,
    config,
    CONFIG_PATH,
    BOTS,
    RESTART_DELAY,
    MAX_RAPID_RESTARTS,
    RAPID_RESTART_WINDOW,
    NETWORK_CHECK_ENABLED,
    NETWORK_CHECK_INTERVAL,
    NETWORK_CHECK_TIMEOUT,
    NETWORK_CHECK_TARGETS,
    WATCHDOG_TOKEN,
    ADMIN_CHANNEL_ID,
    _BOT_COMMANDS,
    _BOT_NAME_TO_PREFIX,
    bot_states,
    bot_states_lock,
    network_monitor_stop,
    network_status_lock,
    normalize_network_targets,
    run_network_checks,
    log_network_results,
    start_network_monitor,
    start_bot,
    stop_bot,
    restart_bot,
    find_bot_name,
    set_notify_hook,
    set_bot_output_hook,
    start_all_enabled_bots,
    start_discord_bot_thread,
    _send_ipc,
)
from RaidBook_Watchdog import restart_discord_bot as _restart_discord_bot

# 버전 및 업데이트 관리
try:
    import version
    import RaidBook_updater as updater
    _UPDATER_AVAILABLE = True
except ImportError:
    _UPDATER_AVAILABLE = False
    version = None
    updater = None

# ── 선택적 의존성 ──────────────────────────────────────────────────────────
try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False


# Windows 토스트 알림 (pystray 없을 때 폴백)
def _win_toast(title: str, msg: str):
    """Windows 10/11 토스트 알림 (PowerShell 사용)."""
    try:
        script = (
            f"Add-Type -AssemblyName System.Windows.Forms;"
            f"$n = New-Object System.Windows.Forms.NotifyIcon;"
            f"$n.Icon = [System.Drawing.SystemIcons]::Information;"
            f"$n.Visible = $true;"
            f"$n.ShowBalloonTip(4000, '{title}', '{msg}', "
            f"[System.Windows.Forms.ToolTipIcon]::Info);"
            f"Start-Sleep -Milliseconds 4500; $n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        pass


from config_helper import (
    ensure_config_from_template,
    ensure_runtime_tree,
    get_app_dir,
    get_config_path,
    get_runtime_dir,
    is_frozen,
)

# ── 로그 큐 핸들러 (GUI 텍스트 위젯으로 전달) ─────────────────────────────
_log_queue: queue.Queue = queue.Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_queue.put_nowait(self.format(record))
        except Exception:
            pass


_queue_handler = _QueueHandler()
_queue_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(_queue_handler)

# ── 필수 설정값 점검 ──────────────────────────────────────────────────────
# 각 봇이 기동/동작하기 위해 반드시 채워져 있어야 하는 값 목록.
# 하나라도 비어 있으면 초기 설정 마법사를 먼저 띄운다.
_REQUIRED_CONFIG_FIELDS = [
    ("watchdog_config.json",   "Watchdog",
     [("token", "토큰")]),
    ("boss_config.json",       "Boss Bot",
     [("token", "토큰")]),
    # 채널 ID는 비워 두면 봇이 시작할 때 자동 생성 (RaidBook_channel_setup)

]


def missing_required_configs() -> list:
    """실행에 반드시 필요한 설정값 중 비어 있는 항목의 라벨 목록을 반환."""
    missing = []
    for fname, bot_label, fields in _REQUIRED_CONFIG_FIELDS:
        try:
            with open(get_config_path(fname), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        for key, label in fields:
            if not cfg.get(key):
                missing.append(f"{bot_label} {label}")
    return missing


# 초기 설정 마법사 필요 여부 (봇 토큰 중 하나라도 미설정 시)
_WIZARD_MODE = bool(missing_required_configs())


# ── 설정·데이터 내보내기 / 가져오기 ──────────────────────────────────────
_KNOWN_CONFIG_FILES = tuple(f for f, _, _ in _REQUIRED_CONFIG_FIELDS) + ("party_config.json",)


def export_settings(zip_path: str) -> int:
    """Config와 Data(롤링 백업 제외)를 ZIP으로 내보낸다. 반환: 파일 수."""
    runtime = get_runtime_dir()
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in ("Config", "Data"):
            base = os.path.join(runtime, folder)
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base):
                if folder == "Data":
                    dirs[:] = [d for d in dirs if d.lower() != "backup"]
                for fn in files:
                    if ".template." in fn:
                        continue
                    full = os.path.join(root, fn)
                    zf.write(full, os.path.relpath(full, runtime))
                    count += 1
    return count


def import_settings(src_path: str) -> int:
    """ZIP 파일 또는 폴더에서 Config/Data를 복원한다. 반환: 복사된 파일 수.

    허용하는 원본 구조:
      - Config/, Data/ 폴더를 포함한 폴더 또는 ZIP (export_settings 결과물 포함)
      - 설정 JSON들이 바로 들어 있는 폴더 (예: 예전 설치본의 Config 폴더 자체)
    복원 전 현재 상태를 Backup/import_backup_<시각>.zip 으로 자동 백업한다.
    """
    runtime = get_runtime_dir()
    tmp = None
    try:
        if os.path.isfile(src_path) and src_path.lower().endswith(".zip"):
            tmp = tempfile.mkdtemp(prefix="raidbook_import_")
            with zipfile.ZipFile(src_path) as zf:
                zf.extractall(tmp)
            src_dir = tmp
        elif os.path.isdir(src_path):
            src_dir = src_path
        else:
            raise ValueError("ZIP 파일 또는 폴더를 선택하세요.")

        cfg_src = os.path.join(src_dir, "Config")
        data_src = os.path.join(src_dir, "Data")
        cfg_src = cfg_src if os.path.isdir(cfg_src) else None
        data_src = data_src if os.path.isdir(data_src) else None
        if cfg_src is None and data_src is None:
            # 폴더 자체가 Config 폴더인 경우 (설정 JSON이 바로 들어 있음)
            if any(os.path.exists(os.path.join(src_dir, f)) for f in _KNOWN_CONFIG_FILES):
                cfg_src = src_dir
            else:
                raise ValueError("원본에서 Config/Data 폴더나 설정 파일을 찾을 수 없습니다.")

        # 덮어쓰기 전 현재 상태 안전 백업
        backup_dir = os.path.join(runtime, "Backup")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_settings(os.path.join(backup_dir, f"import_backup_{ts}.zip"))

        copied = 0

        def _copy_tree(s, d, skip_backup=False):
            nonlocal copied
            for root, dirs, files in os.walk(s):
                if skip_backup:
                    dirs[:] = [x for x in dirs if x.lower() != "backup"]
                rel = os.path.relpath(root, s)
                dst_root = d if rel == "." else os.path.join(d, rel)
                os.makedirs(dst_root, exist_ok=True)
                for fn in files:
                    if ".template." in fn:
                        continue
                    shutil.copy2(os.path.join(root, fn), os.path.join(dst_root, fn))
                    copied += 1

        if cfg_src:
            _copy_tree(cfg_src, os.path.join(runtime, "Config"))
        if data_src:
            _copy_tree(data_src, os.path.join(runtime, "Data"), skip_backup=True)
        return copied
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

# ── 알림 큐 (GUI 스레드에서 소비) ─────────────────────────────────────────
_notify_queue: queue.Queue = queue.Queue()


def _notify(title: str, msg: str):
    """어디서든 호출 → GUI 스레드가 실제 알림 처리."""
    _notify_queue.put_nowait((title, msg))


# ── 봇별 상세 로그 큐  {bot_name: queue.Queue[str]} ───────────────────────
BOT_LOG_MAX = 5000          # 봇당 최대 보관 줄 수
bot_log_queues: dict = {b["name"]: queue.Queue() for b in config.get("bots", [])}
bot_log_lines: dict = {b["name"]: [] for b in config.get("bots", [])}
bot_log_lock = threading.Lock()


def _bot_log(name: str, line: str):
    """봇 stdout 한 줄을 큐와 버퍼에 저장 + 메인 로그에도 전달."""
    ts = datetime.now().strftime("%H:%M:%S")
    text = f"[{ts}] {line}"                    # 봇별 상세 로그용 (이름 없음)

    # 메인 로그용: 라인에 이미 타임스탬프가 있으면 중복 방지
    # (discord.py 등이 자체 포매터 타임스탬프를 출력하는 경우)
    # _write_log_line은 첫 번째 [...] 을 시간 태그(회색)로 스타일링하므로
    # 봇 이름을 맨 앞에 두어 자연스럽게 출력
    if line.startswith("["):
        main_text = f"[{name}] {line}"         # [Boss Bot] [기존 타임스탬프] ...
    else:
        main_text = f"[{ts}] [{name}] {line}"  # [HH:MM:SS] [Boss Bot] 메시지

    with bot_log_lock:
        lines = bot_log_lines[name]
        lines.append(text)
        if len(lines) > BOT_LOG_MAX:
            del lines[:1000]
    bot_log_queues[name].put_nowait(text)
    _log_queue.put_nowait(main_text)            # 메인 로그 창에도 표시


# GUI의 알림/로그 처리를 공유 코어에 등록 — 코어는 tkinter를 전혀 모른다.
set_notify_hook(_notify)
set_bot_output_hook(_bot_log)

# 색상 팔레트
_C = {
    "bg":           "#1e1e2e",   # 전체 배경 (어두운 네이비)
    "panel":        "#2a2a3e",   # 패널 배경
    "border":       "#44445a",   # 테두리
    "text":         "#cdd6f4",   # 기본 텍스트
    "text_dim":     "#6c7086",   # 흐린 텍스트
    "green":        "#a6e3a1",   # 실행중
    "yellow":       "#f9e2af",   # 대기
    "red":          "#f38ba8",   # 중지
    "blue":         "#89b4fa",   # 버튼/강조
    "blue_hover":   "#74c7ec",
    "btn_green":    "#40a02b",
    "btn_red":      "#d20f39",
    "btn_yellow":   "#df8e1d",
    "btn_blue":     "#1e66f5",
    "log_bg":       "#181825",
    "log_text":     "#cdd6f4",
    "log_time":     "#6c7086",
    "log_warn":     "#f9e2af",
    "log_err":      "#f38ba8",
}


class _Tooltip:
    """마우스 호버 시 작은 팝업 툴팁."""
    _DELAY_MS = 500   # 표시까지 대기 시간
    _PAD      = 6

    def __init__(self, widget: tk.Widget, text: str):
        self._widget  = widget
        self._text    = text
        self._job     = None
        self._tip_win = None
        widget.bind("<Enter>",   self._on_enter, add="+")
        widget.bind("<Leave>",   self._on_leave, add="+")
        widget.bind("<Button>",  self._on_leave, add="+")

    def _on_enter(self, _evt=None):
        self._cancel()
        self._job = self._widget.after(self._DELAY_MS, self._show)

    def _on_leave(self, _evt=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None

    def _show(self):
        if self._tip_win:
            return
        x = self._widget.winfo_rootx() + self._widget.winfo_width() // 2
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4

        self._tip_win = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)   # 제목바 제거
        tw.wm_attributes("-topmost", True)
        tw.wm_geometry(f"+{x}+{y}")

        bg = "#313244"
        lbl = tk.Label(
            tw, text=self._text,
            font=("Segoe UI", 9),
            bg=bg, fg="#cdd6f4",
            relief="flat", bd=0,
            padx=self._PAD, pady=self._PAD - 2,
            justify="left",
            wraplength=260,
        )
        lbl.pack()
        # 테두리 효과
        tw.configure(bg="#44445a")
        lbl.pack(padx=1, pady=1)

    def _hide(self):
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None


class WatchdogApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RaidBook Watchdog")
        self.configure(bg=_C["bg"])
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Images", "Icon", "WatchDog.ico")
        if os.path.exists(_icon_path):
            try:
                self.iconbitmap(_icon_path)
            except Exception:
                pass
        self.minsize(780, 560)
        self.resizable(True, True)

        # 폰트
        self._font_title  = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self._font_body   = tkfont.Font(family="Segoe UI", size=10)
        self._font_small  = tkfont.Font(family="Segoe UI", size=9)
        self._font_mono   = tkfont.Font(family="Consolas",  size=9)
        self._font_status = tkfont.Font(family="Segoe UI", size=10, weight="bold")

        self._bot_row_widgets: dict = {}  # name → {label_status, btn_start, btn_stop, btn_restart}
        self._net_labels: dict      = {}  # host:port → label

        self._log_level_filter = "all"   # "all" | "warn" | "err"
        self._log_buffer: list = []       # [(line_str, level_str)]
        self._render_after_id  = None     # 청크 렌더링 after() ID
        self._tray_icon  = None           # _finish_init 전에도 참조될 수 있으므로 미리 초기화

        # 필수 설정 누락이면 메인 창 숨기고 초기 설정 마법사 먼저 실행
        if _WIZARD_MODE:
            self.withdraw()
            self._show_setup_wizard(on_complete=self._finish_init)
        else:
            self._finish_init()

        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.bind("<Unmap>", self._on_unmap)

    # ── UI 빌드 ────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── config 미설정 경고 배너 ───────────────────────────────────────
        self._config_banner = None
        warnings = missing_required_configs()
        if warnings:
            banner = tk.Frame(self, bg="#4a1a1a")
            banner.pack(fill="x")
            self._config_banner = banner
            tk.Label(banner, text="⚠  필수 설정 누락:  " + ",  ".join(warnings),
                     font=self._font_small, bg="#4a1a1a", fg="#f38ba8",
                     anchor="w", padx=12, pady=4).pack(side="left", fill="x", expand=True)
            fix_btn = tk.Button(banner, text="🧭 지금 설정", command=self._open_config_wizard,
                                font=self._font_small, bg=_C["btn_blue"], fg="white",
                                relief="flat", padx=10, pady=2, cursor="hand2")
            fix_btn.pack(side="right", padx=8, pady=3)
            _Tooltip(fix_btn, "설정 마법사를 열어 누락된 봇 토큰을 채웁니다.\n저장하면 봇이 자동 재시작되어 즉시 적용됩니다.")

        # ── 헤더 ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=_C["bg"])
        hdr.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(hdr, text="🛡 RaidBook Watchdog", font=self._font_title,
                 bg=_C["bg"], fg=_C["blue"]).pack(side="left")

        # 전체 버튼들 (헤더 오른쪽)
        for label, cmd, color, tip in [
            ("▶ 전체 시작",   lambda: self._all_action("start"),   _C["btn_green"],  "활성화된 모든 봇을 시작합니다."),
            ("⛔ 전체 중지",   lambda: self._all_action("stop"),    _C["btn_red"],    "실행 중인 모든 봇을 중지합니다.\n봇 프로세스가 종료되며 자동 재시작도 멈춥니다."),
            ("🔄 전체 재시작", lambda: self._all_action("restart"), _C["btn_yellow"], "모든 봇을 중지한 뒤 순서대로 다시 시작합니다."),
        ]:
            btn = tk.Button(hdr, text=label, command=cmd, font=self._font_small,
                            bg=color, fg="white", activebackground=_C["blue_hover"],
                            relief="flat", padx=8, pady=3, cursor="hand2")
            btn.pack(side="right", padx=4)
            _Tooltip(btn, tip)

        # ⚙ 설정 버튼
        cfg_btn = tk.Button(hdr, text="⚙", command=self._open_settings,
                            font=self._font_body, bg=_C["panel"], fg=_C["text_dim"],
                            activebackground=_C["border"], relief="flat",
                            padx=8, pady=2, cursor="hand2")
        cfg_btn.pack(side="left", padx=(12, 0))
        _Tooltip(cfg_btn, "런타임 중 즉시 적용 가능한 설정을 변경합니다.")

        # 🔄 업데이트 버튼
        if _UPDATER_AVAILABLE and version:
            update_btn = tk.Button(hdr, text="🔄", command=self._check_updates,
                                   font=self._font_body, bg=_C["panel"], fg=_C["text_dim"],
                                   activebackground=_C["border"], relief="flat",
                                   padx=8, pady=2, cursor="hand2")
            update_btn.pack(side="left", padx=4)
            _Tooltip(update_btn, f"업데이트 확인 (현재 v{version.VERSION})")

        # ── 구분선 ────────────────────────────────────────────────────────
        tk.Frame(self, bg=_C["border"], height=1).pack(fill="x", padx=14)

        # ── 메인 레이아웃 (좌: 봇 + 네트워크 | 우: 로그) ──────────────────
        main = tk.Frame(self, bg=_C["bg"])
        main.pack(fill="both", expand=True, padx=14, pady=8)
        main.columnconfigure(0, weight=0, minsize=360)
        main.columnconfigure(1, weight=1, minsize=320)
        main.rowconfigure(0, weight=1)

        left  = tk.Frame(main, bg=_C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        right = tk.Frame(main, bg=_C["bg"])
        right.grid(row=0, column=1, sticky="nsew")

        # ── 봇 패널 ───────────────────────────────────────────────────────
        self._build_bot_panel(left)
        # ── 네트워크 패널 ─────────────────────────────────────────────────
        self._build_network_panel(left)
        # ── 로그 패널 ─────────────────────────────────────────────────────
        self._build_log_panel(right)

        # ── 상태바 ────────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="준비")
        sb = tk.Label(self, textvariable=self._status_var, font=self._font_small,
                      bg=_C["panel"], fg=_C["text_dim"], anchor="w", padx=8)
        sb.pack(fill="x", side="bottom")

    def _panel_frame(self, parent, title):
        """제목+테두리가 있는 패널 프레임 반환."""
        outer = tk.Frame(parent, bg=_C["border"], bd=0)
        outer.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(outer, bg=_C["panel"], bd=0)
        inner.pack(fill="both", padx=1, pady=1)
        lbl = tk.Label(inner, text=title, font=self._font_small,
                       bg=_C["panel"], fg=_C["text_dim"], anchor="w")
        lbl.pack(fill="x", padx=10, pady=(6, 2))
        tk.Frame(inner, bg=_C["border"], height=1).pack(fill="x", padx=10)
        body = tk.Frame(inner, bg=_C["panel"])
        body.pack(fill="both", padx=8, pady=6)
        return body

    def _build_bot_panel(self, parent):
        body = self._panel_frame(parent, "봇 상태")
        for b in BOTS:
            name    = b["name"]
            enabled = b.get("enabled", True)

            # ── 행 컨테이너 (2줄: 메인 + 서브정보) ────────────────────────
            outer = tk.Frame(body, bg=_C["panel"])
            outer.pack(fill="x", pady=2)

            row = tk.Frame(outer, bg=_C["panel"])
            row.pack(fill="x")

            # 상태 인디케이터 (●)
            indicator = tk.Label(row, text="●", font=self._font_body,
                                 bg=_C["panel"], fg=_C["text_dim"], width=2)
            indicator.pack(side="left")

            # ── 자동 재시작 토글 ─────────────────────────────────────────
            # Checkbutton 대신 커스텀 Label 사용 (어두운 테마에서 체크 표시가
            # selectcolor에 묻혀 보이지 않는 문제 해결)
            enabled_var = tk.BooleanVar(value=enabled)

            def _make_toggle(n, var, parent_row):
                def _upd():
                    v = var.get()
                    lbl.configure(
                        text=" ✓ " if v else " ✗ ",
                        fg=_C["green"] if v else _C["red"],
                    )
                def _click(_evt=None):
                    var.set(not var.get())
                    _upd()
                    self._toggle_bot_enabled(n, var)
                lbl = tk.Label(
                    parent_row,
                    text=" ✓ " if var.get() else " ✗ ",
                    font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                    fg=_C["green"] if var.get() else _C["red"],
                    bg=_C["panel"], cursor="hand2",
                )
                lbl.bind("<Button-1>", _click)
                lbl.pack(side="left", padx=(0, 1))
                _Tooltip(lbl, f"✓=자동 재시작 활성  ✗=비활성\n클릭으로 {n} 자동 재시작 켜기/끄기.")
                return lbl, _upd

            toggle_lbl, toggle_upd = _make_toggle(name, enabled_var, row)

            name_lbl = tk.Label(row, text=name, font=self._font_body,
                                bg=_C["panel"], fg=_C["text"], width=14, anchor="w")
            name_lbl.pack(side="left", padx=(0, 4))

            # ── 제어 버튼 (side="right" → 마지막에 pack할수록 더 왼쪽) ────
            # pack 순서: start → stop → restart → 로그 → 명령
            # 화면 표시 순서(왼→오): 명령 | 로그 | restart | stop | start
            btn_start   = self._small_btn(row, "▶",   lambda n=name: self._bot_action("start",   n), _C["btn_green"],
                                          tooltip=f"{name}을(를) 시작합니다.\n중지 상태일 때만 동작합니다.")
            btn_stop    = self._small_btn(row, "■",   lambda n=name: self._bot_action("stop",    n), _C["btn_red"],
                                          tooltip=f"{name}을(를) 중지합니다.\n프로세스를 종료하고 자동 재시작을 막습니다.")
            btn_restart = self._small_btn(row, "↺",   lambda n=name: self._bot_action("restart", n), _C["btn_yellow"],
                                          tooltip=f"{name}을(를) 재시작합니다.\n현재 프로세스를 종료 후 2초 뒤 다시 시작합니다.")

            # 상세 로그 버튼 — 이모지 대신 한글 텍스트 사용 (렌더링 안정)
            btn_log = self._small_btn(row, "로그", lambda n=name: self._open_bot_log(n), _C["panel"],
                                      tooltip=f"{name}의 상세 로그를 팝업으로 봅니다.\n참석/취소, 메시지 복구 등 봇 내부 출력이 표시됩니다.")
            btn_log.configure(fg=_C["yellow"], bd=1, relief="groove")

            # 명령어 목록 버튼 (prefix 있는 봇만)
            prefix = _BOT_NAME_TO_PREFIX.get(name)
            if prefix and prefix in _BOT_COMMANDS:
                btn_cmds = self._small_btn(row, "명령", lambda n=name: self._open_cmd_panel(n), _C["panel"],
                                           tooltip=f"{name} 명령어 목록을 팝업으로 엽니다.\n클릭으로 입력창에 삽입, 더블클릭으로 즉시 실행.")
                btn_cmds.configure(fg=_C["blue"], bd=1, relief="groove")
            else:
                btn_cmds = None

            # ── 서브 행 (상태 텍스트 + 가동시간 + 재시작 횟수) ───────────
            # status_lbl을 sub_row로 이동 → main row 공간 확보 (버튼 잘림 방지)
            sub_row = tk.Frame(outer, bg=_C["panel"])
            sub_row.pack(fill="x", padx=(26, 0))

            status_lbl = tk.Label(sub_row, text="—", font=self._font_small,
                                  bg=_C["panel"], fg=_C["text_dim"], anchor="w")
            status_lbl.pack(side="left", padx=(0, 8))

            uptime_lbl = tk.Label(sub_row, text="", font=self._font_small,
                                  bg=_C["panel"], fg=_C["text_dim"], anchor="w")
            uptime_lbl.pack(side="left")

            self._bot_row_widgets[name] = {
                "indicator":   indicator,
                "status_lbl":  status_lbl,
                "uptime_lbl":  uptime_lbl,
                "enabled_var": enabled_var,
                "toggle_upd":  toggle_upd,
                "btn_start":   btn_start,
                "btn_stop":    btn_stop,
                "btn_restart": btn_restart,
                "btn_cmds":    btn_cmds,
            }

    def _small_btn(self, parent, text, command, bg, tooltip: str = ""):
        btn = tk.Button(parent, text=text, command=command, font=self._font_small,
                        bg=bg, fg="white", activebackground=_C["blue_hover"],
                        relief="flat", padx=6, pady=1, cursor="hand2")
        btn.pack(side="right", padx=2)
        if tooltip:
            _Tooltip(btn, tooltip)
        return btn

    def _build_network_panel(self, parent):
        body = self._panel_frame(parent, "네트워크 상태")

        btn_row = tk.Frame(body, bg=_C["panel"])
        btn_row.pack(fill="x", pady=(0, 4))
        net_btn = tk.Button(btn_row, text="🔍 지금 확인", command=self._netcheck_now,
                  font=self._font_small, bg=_C["btn_blue"], fg="white",
                  relief="flat", padx=8, pady=2, cursor="hand2")
        net_btn.pack(side="left")
        _Tooltip(net_btn, "Discord 서버에 TCP 연결을 즉시 테스트합니다.\n결과는 로그와 이 패널에 표시됩니다.")
        self._net_checked_lbl = tk.Label(btn_row, text="", font=self._font_small,
                                         bg=_C["panel"], fg=_C["text_dim"])
        self._net_checked_lbl.pack(side="left", padx=8)

        self._net_body = tk.Frame(body, bg=_C["panel"])
        self._net_body.pack(fill="x")
        # 초기 빈 행은 첫 결과 도착 후 채워짐

    def _build_log_panel(self, parent):
        header = tk.Frame(parent, bg=_C["bg"])
        header.pack(fill="x")
        tk.Label(header, text="로그", font=self._font_small,
                 bg=_C["bg"], fg=_C["text_dim"]).pack(side="left", padx=4)
        clear_btn = tk.Button(header, text="🗑 지우기", command=self._clear_log,
                  font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                  relief="flat", padx=6, pady=1, cursor="hand2")
        clear_btn.pack(side="right", padx=4)
        _Tooltip(clear_btn, "로그 창의 모든 내용을 지웁니다.\n파일 로그에는 영향 없습니다.")

        # ── 로그 레벨 필터 버튼 ───────────────────────────────────────────
        self._filter_btns: dict = {}
        for level, label, tip in [
            ("all",  "전체",    "모든 로그를 표시합니다."),
            ("warn", "경고 이상", "경고 및 오류 로그만 표시합니다."),
            ("err",  "오류만",  "오류 로그만 표시합니다."),
        ]:
            btn = tk.Button(header, text=label,
                            command=lambda lv=level: self._set_log_filter(lv),
                            font=self._font_small,
                            bg=_C["btn_blue"] if level == "all" else _C["panel"],
                            fg="white" if level == "all" else _C["text_dim"],
                            relief="flat", padx=8, pady=1, cursor="hand2")
            btn.pack(side="left", padx=2)
            _Tooltip(btn, tip)
            self._filter_btns[level] = btn

        # ── 검색 바 ────────────────────────────────────────────────────────
        self._search_visible = False
        self._search_matches: list = []   # 매칭된 위치 목록
        self._search_idx:     int  = -1   # 현재 선택 인덱스

        self._search_bar = tk.Frame(parent, bg=_C["panel"])
        # 초기에는 숨김 — Ctrl+F 로 토글

        tk.Label(self._search_bar, text="🔍", font=self._font_small,
                 bg=_C["panel"], fg=_C["text_dim"], padx=6).pack(side="left")

        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            self._search_bar, textvariable=self._search_var,
            font=self._font_mono, bg=_C["log_bg"], fg=_C["text"],
            insertbackground=_C["text"], relief="flat", bd=0, width=22,
        )
        self._search_entry.pack(side="left", ipady=3, padx=(0, 4))

        self._search_count_lbl = tk.Label(
            self._search_bar, text="", font=self._font_small,
            bg=_C["panel"], fg=_C["text_dim"], width=10, anchor="w")
        self._search_count_lbl.pack(side="left")

        self._case_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self._search_bar, text="Aa", variable=self._case_var,
                       command=self._search_apply,
                       font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                       selectcolor=_C["bg"], activebackground=_C["panel"],
                       relief="flat").pack(side="left", padx=4)
        _Tooltip(self._search_bar.children.get(list(self._search_bar.children)[-1],
                 tk.Label()), "대소문자 구분 검색")

        for txt, cmd, tip in [
            ("▲", self._search_prev, "이전 결과 (Shift+Enter)"),
            ("▼", self._search_next, "다음 결과 (Enter)"),
        ]:
            b = tk.Button(self._search_bar, text=txt, command=cmd,
                          font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                          relief="flat", padx=6, pady=1, cursor="hand2")
            b.pack(side="left", padx=1)
            _Tooltip(b, tip)

        close_s = tk.Button(self._search_bar, text="✕", command=self._search_hide,
                            font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                            relief="flat", padx=6, pady=1, cursor="hand2")
        close_s.pack(side="right", padx=4)
        _Tooltip(close_s, "검색 닫기 (Esc)")

        # 검색어 변경 시 자동 적용
        self._search_var.trace_add("write", lambda *_: self._search_apply())

        # 로그 텍스트 + 스크롤바
        log_frame = tk.Frame(parent, bg=_C["bg"])
        log_frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(
            log_frame, font=self._font_mono,
            bg=_C["log_bg"], fg=_C["log_text"],
            insertbackground=_C["text"],
            selectbackground=_C["border"],
            relief="flat", bd=0, wrap="word", state="disabled"
        )
        sb_v = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb_v.set)

        # ── 다크 테마 스크롤바 스타일 (전역 적용 — 모든 팝업에도 반영됨) ──
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar",
                        troughcolor=_C["panel"],
                        background=_C["border"],
                        arrowcolor=_C["text_dim"],
                        bordercolor=_C["panel"],
                        darkcolor=_C["border"],
                        lightcolor=_C["border"],
                        gripcount=0,
                        relief="flat",
                        width=10)
        style.map("Vertical.TScrollbar",
                  background=[("active",  "#585b70"),   # hover — 밝은 회색
                               ("pressed", "#7f849c"),  # 클릭 — 더 밝게
                               ("!active", _C["border"])])
        style.configure("Horizontal.TScrollbar",
                        troughcolor=_C["panel"],
                        background=_C["border"],
                        arrowcolor=_C["text_dim"],
                        bordercolor=_C["panel"],
                        darkcolor=_C["border"],
                        lightcolor=_C["border"],
                        gripcount=0,
                        relief="flat",
                        width=10)
        style.map("Horizontal.TScrollbar",
                  background=[("active",  "#585b70"),
                               ("pressed", "#7f849c"),
                               ("!active", _C["border"])])

        sb_v.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)

        # 태그 색상
        self._log_text.tag_config("time",        foreground=_C["log_time"])
        self._log_text.tag_config("warn",        foreground=_C["log_warn"])
        self._log_text.tag_config("err",         foreground=_C["log_err"])
        self._log_text.tag_config("ok",          foreground=_C["green"])
        self._log_text.tag_config("search_hi",   background="#4a4a00", foreground="#ffff88")  # 검색 일반 하이라이트
        self._log_text.tag_config("search_cur",  background="#cc8800", foreground="#ffffff")  # 현재 선택 하이라이트

        # 키 바인딩
        self.bind_all("<Control-f>", lambda e: self._search_show())
        self._log_text.bind("<Escape>",    lambda e: self._search_hide())
        self._search_entry.bind("<Escape>",      lambda e: self._search_hide())
        self._search_entry.bind("<Return>",      lambda e: self._search_next())
        self._search_entry.bind("<Shift-Return>",lambda e: self._search_prev())

        self._log_autoscroll = True

        # ── 명령어 입력창 ──────────────────────────────────────────────────
        cmd_frame = tk.Frame(parent, bg=_C["panel"], pady=4)
        cmd_frame.pack(fill="x", side="bottom")

        tk.Label(cmd_frame, text=">", font=self._font_mono,
                 bg=_C["panel"], fg=_C["blue"], padx=6).pack(side="left")

        self._cmd_var = tk.StringVar()
        self._cmd_entry = tk.Entry(
            cmd_frame, textvariable=self._cmd_var,
            font=self._font_mono,
            bg=_C["log_bg"], fg=_C["text"],
            insertbackground=_C["text"],
            relief="flat", bd=0
        )
        self._cmd_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._cmd_entry.bind("<Return>", lambda e: self._run_cmd())
        self._cmd_entry.bind("<Up>",     lambda e: self._cmd_history_prev())
        self._cmd_entry.bind("<Down>",   lambda e: self._cmd_history_next())

        run_btn = tk.Button(cmd_frame, text="실행", command=self._run_cmd,
                  font=self._font_small, bg=_C["btn_blue"], fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2")
        run_btn.pack(side="right", padx=6)
        _Tooltip(run_btn, "입력한 명령어를 실행합니다.\nEnter 키로도 실행할 수 있습니다.\n↑↓ 키로 이전 명령어를 불러올 수 있습니다.")

        # 명령어 히스토리
        self._cmd_history: list[str] = []
        self._cmd_history_idx: int = -1

    # ── 주기적 업데이트 ────────────────────────────────────────────────────
    def _schedule_status_poll(self):
        try:
            self._refresh_bot_status()
            self._refresh_network_status()
        except Exception:
            pass
        finally:
            self.after(1500, self._schedule_status_poll)

    def _schedule_log_poll(self):
        try:
            self._flush_log_queue()
        except Exception:
            pass
        finally:
            self.after(200, self._schedule_log_poll)

    def _schedule_notify_poll(self):
        try:
            while True:
                title, msg = _notify_queue.get_nowait()
                self._dispatch_notify(title, msg)
        except queue.Empty:
            pass
        finally:
            self.after(500, self._schedule_notify_poll)

    def _dispatch_notify(self, title: str, msg: str):
        log.info(f"[알림] {title}: {msg}")
        if _TRAY_AVAILABLE and self._tray_icon:
            try:
                self._tray_icon.notify(msg, title)
                return
            except Exception:
                pass
        _win_toast(title, msg)

    def _refresh_bot_status(self):
        with bot_states_lock:
            states_copy = {k: dict(v) for k, v in bot_states.items()}

        now = datetime.now()
        running_count = 0
        for b in BOTS:
            name = b["name"]
            widgets = self._bot_row_widgets.get(name)
            if not widgets:
                continue
            state         = states_copy.get(name, {})
            process       = state.get("process")
            stopped       = state.get("stopped", True)
            rapid_limit   = state.get("rapid_limit", False)
            start_time    = state.get("start_time")
            restart_count = state.get("restart_count", 0)

            if rapid_limit:
                color  = _C["red"]
                status = "⚠ 감시 중단"
            elif stopped:
                color  = _C["red"]
                status = "중지"
            elif process and process.poll() is None:
                color  = _C["green"]
                status = f"실행중  PID {process.pid}"
                running_count += 1
            else:
                color  = _C["yellow"]
                status = "재시작 대기"

            widgets["indicator"].configure(fg=color)
            widgets["status_lbl"].configure(text=status, fg=color)

            # 가동시간 + 재시작 횟수 서브 정보
            sub_parts = []
            if start_time and process and process.poll() is None:
                delta  = now - start_time
                h, rem = divmod(int(delta.total_seconds()), 3600)
                m, s   = divmod(rem, 60)
                if h:
                    sub_parts.append(f"가동 {h}시간 {m}분")
                elif m:
                    sub_parts.append(f"가동 {m}분 {s}초")
                else:
                    sub_parts.append(f"가동 {s}초")
            if restart_count > 0:
                sub_parts.append(f"재시작 {restart_count}회")
            if rapid_limit:
                sub_parts.append("수동 시작 필요")
            if "uptime_lbl" in widgets:
                widgets["uptime_lbl"].configure(
                    text="  " + "  │  ".join(sub_parts) if sub_parts else "",
                    fg=_C["red"] if rapid_limit else _C["text_dim"]
                )

        total = len(BOTS)
        self._status_var.set(
            f"실행중 {running_count}/{total}  |  {now.strftime('%H:%M:%S')}"
        )

    def _refresh_network_status(self):
        # last_network_results/last_network_checked_at는 공유 코어의 run_network_checks()가
        # global 재대입으로 갱신하므로, 항상 _core를 통해 최신값을 읽어야 한다.
        with network_status_lock:
            results    = list(_core.last_network_results)
            checked_at = _core.last_network_checked_at

        if checked_at:
            self._net_checked_lbl.configure(
                text=f"최근: {checked_at.strftime('%H:%M:%S')}")

        # 기존 위젯 제거 후 재생성
        for w in self._net_body.winfo_children():
            w.destroy()
        self._net_labels.clear()

        for r in results:
            row = tk.Frame(self._net_body, bg=_C["panel"])
            row.pack(fill="x", pady=1)
            ok_text = f"✅  {r['elapsed_ms']}ms" if r["ok"] else f"❌  실패"
            color   = _C["green"] if r["ok"] else _C["red"]
            tk.Label(row, text=r["name"], font=self._font_small,
                     bg=_C["panel"], fg=_C["text"], width=20, anchor="w").pack(side="left", padx=(0, 6))
            tk.Label(row, text=ok_text, font=self._font_small,
                     bg=_C["panel"], fg=color, anchor="w").pack(side="left")

    # ── 로그 처리 ──────────────────────────────────────────────────────────
    def _flush_log_queue(self):
        count = 0
        while count < 50:  # 한 번에 최대 50줄
            try:
                line = _log_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._append_log(line)
            except Exception:
                pass
            count += 1

    @staticmethod
    def _log_level(line: str) -> str:
        """로그 한 줄의 레벨을 반환: 'err' | 'warn' | 'ok' | ''"""
        rest = line[line.index("]") + 1:] if "]" in line else line
        if any(kw in rest for kw in ("오류", "실패", "❌", "ERROR")):
            return "err"
        if any(kw in rest for kw in ("경고", "재시작 대기", "WARN", "⚠")):
            return "warn"
        if any(kw in rest for kw in ("시작", "연결", "OK", "✅", "완료")):
            return "ok"
        return ""

    def _line_passes_filter(self, level: str) -> bool:
        f = self._log_level_filter
        if f == "all":  return True
        if f == "err":  return level == "err"
        if f == "warn": return level in ("err", "warn")
        return True

    def _set_log_filter(self, level: str):
        self._log_level_filter = level
        # 버튼 스타일 갱신
        for lv, btn in self._filter_btns.items():
            active = lv == level
            btn.configure(
                bg=_C["btn_blue"] if active else _C["panel"],
                fg="white"        if active else _C["text_dim"],
            )
        # 기존 버퍼 재렌더링 — 청크 방식으로 UI 블로킹 방지
        w = self._log_text
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.configure(state="disabled")
        # 진행 중인 청크 렌더링이 있으면 취소
        if hasattr(self, "_render_after_id") and self._render_after_id:
            self.after_cancel(self._render_after_id)
            self._render_after_id = None
        filtered = [(line, lv) for line, lv in self._log_buffer
                    if self._line_passes_filter(lv)]
        self._render_log_chunk(filtered, 0)

    def _render_log_chunk(self, lines: list, offset: int, chunk: int = 150):
        """로그 버퍼를 chunk 단위로 나눠 렌더링해 UI 블로킹을 방지한다."""
        batch = lines[offset:offset + chunk]
        for line, lv in batch:
            self._write_log_line(line, lv)
        next_offset = offset + chunk
        if next_offset < len(lines):
            self._render_after_id = self.after(
                8, lambda: self._render_log_chunk(lines, next_offset, chunk)
            )
        else:
            self._render_after_id = None
            # 렌더링 완료 후 스크롤을 맨 아래로
            self._log_text.see("end")

    def _write_log_line(self, line: str, level: str):
        """실제로 텍스트 위젯에 한 줄 삽입 (state 관리 포함)."""
        w = self._log_text
        w.configure(state="normal")
        try:
            if line.startswith("[") and "]" in line:
                bracket_end = line.index("]") + 1
                w.insert("end", line[:bracket_end], "time")
                rest = line[bracket_end:]
            else:
                rest = line
            tag = level if level else ""
            w.insert("end", rest + "\n", tag)
            if self._log_autoscroll:
                w.see("end")
            line_count = int(w.index("end-1c").split(".")[0])
            if line_count > 5000:
                w.delete("1.0", "1001.0")
                # 버퍼도 동기화
                if len(self._log_buffer) > 5000:
                    del self._log_buffer[:1000]
        finally:
            w.configure(state="disabled")

    def _append_log(self, line: str):
        level = self._log_level(line)
        # 버퍼에 저장
        self._log_buffer.append((line, level))
        if len(self._log_buffer) > 5000:
            del self._log_buffer[:1000]
        # 필터 통과 시만 위젯에 표시
        if self._line_passes_filter(level):
            self._write_log_line(line, level)

    def _clear_log(self):
        self._log_buffer.clear()
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")
        self._search_matches.clear()
        self._search_idx = -1
        self._search_count_lbl.configure(text="")

    # ── 로그 검색 ──────────────────────────────────────────────────────────
    def _search_show(self):
        if not self._search_visible:
            self._search_bar.pack(fill="x", before=self._log_text.master)
            self._search_visible = True
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")
        self._search_apply()

    def _search_hide(self):
        if self._search_visible:
            self._search_bar.pack_forget()
            self._search_visible = False
        self._log_text.tag_remove("search_hi",  "1.0", "end")
        self._log_text.tag_remove("search_cur", "1.0", "end")
        self._search_matches.clear()
        self._search_idx = -1
        self._search_count_lbl.configure(text="")
        self._cmd_entry.focus_set()

    def _search_apply(self):
        """검색어로 전체 하이라이트 재적용."""
        w   = self._log_text
        w.tag_remove("search_hi",  "1.0", "end")
        w.tag_remove("search_cur", "1.0", "end")
        self._search_matches.clear()
        self._search_idx = -1

        keyword = self._search_var.get()
        if not keyword:
            self._search_count_lbl.configure(text="")
            return

        nocase  = not self._case_var.get()
        start   = "1.0"
        while True:
            pos = w.search(keyword, start, stopindex="end", nocase=nocase)
            if not pos:
                break
            end = f"{pos}+{len(keyword)}c"
            w.tag_add("search_hi", pos, end)
            self._search_matches.append((pos, end))
            start = end

        total = len(self._search_matches)
        if total == 0:
            self._search_count_lbl.configure(text="없음", fg=_C["red"])
        else:
            self._search_count_lbl.configure(text=f"1 / {total}", fg=_C["green"])
            self._search_idx = 0
            self._search_highlight_cur()

    def _search_highlight_cur(self):
        """현재 선택 항목을 강조하고 스크롤."""
        w = self._log_text
        w.tag_remove("search_cur", "1.0", "end")
        if not self._search_matches or self._search_idx < 0:
            return
        pos, end = self._search_matches[self._search_idx]
        w.tag_add("search_cur", pos, end)
        w.see(pos)
        total = len(self._search_matches)
        self._search_count_lbl.configure(
            text=f"{self._search_idx + 1} / {total}", fg=_C["green"])

    def _search_next(self):
        if not self._search_matches:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_matches)
        self._search_highlight_cur()

    def _search_prev(self):
        if not self._search_matches:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_matches)
        self._search_highlight_cur()

    # ── 명령어 처리 ────────────────────────────────────────────────────────
    def _run_cmd(self):
        raw = self._cmd_var.get().strip()
        if not raw:
            return

        # ! 접두사 허용  (!boss history → boss history)
        cmd = raw.lstrip("!")

        # 히스토리 저장
        if not self._cmd_history or self._cmd_history[-1] != raw:
            self._cmd_history.append(raw)
        self._cmd_history_idx = -1
        self._cmd_var.set("")

        log.info(f"[CMD] > {raw}")

        parts  = cmd.split(None, 2)
        action = parts[0].lower()

        # ── 봇 IPC 명령어 (boss) ─────────────────────────────────────────
        _IPC_PREFIXES = {"boss": "boss"}
        if action in _IPC_PREFIXES:
            bot_key  = _IPC_PREFIXES[action]
            sub_cmd  = parts[1] if len(parts) > 1 else ""
            sub_args = parts[2].split() if len(parts) > 2 else []
            if not sub_cmd:
                log.info(f"[CMD] 사용법:  {action} [명령어] [인자...]")
                log.info(f"[CMD] 명령어 목록은 봇 행의 📋 버튼을 눌러 확인하세요.")
                return
            threading.Thread(
                target=self._ipc_call,
                args=(bot_key, sub_cmd, sub_args),
                daemon=True
            ).start()
            return

        # ── Watchdog 자체 명령어 ──────────────────────────────────────────
        target = parts[1].strip() if len(parts) > 1 else ""

        if action == "status":
            threading.Thread(target=self._cmd_status, daemon=True).start()

        elif action == "restart":
            name = "all" if not target or target.lower() == "all" else find_bot_name(target)
            if not name:
                log.info(f"[CMD] 봇을 찾을 수 없음: {target}")
                return
            if name == "all":
                threading.Thread(target=self._all_action, args=("restart",), daemon=True).start()
            else:
                threading.Thread(target=self._bot_action, args=("restart", name), daemon=True).start()

        elif action == "stop":
            name = "all" if not target or target.lower() == "all" else find_bot_name(target)
            if not name:
                log.info(f"[CMD] 봇을 찾을 수 없음: {target}")
                return
            if name == "all":
                threading.Thread(target=self._all_action, args=("stop",), daemon=True).start()
            else:
                threading.Thread(target=self._bot_action, args=("stop", name), daemon=True).start()

        elif action == "start":
            name = "all" if not target or target.lower() == "all" else find_bot_name(target)
            if not name:
                log.info(f"[CMD] 봇을 찾을 수 없음: {target}")
                return
            if name == "all":
                threading.Thread(target=self._all_action, args=("start",), daemon=True).start()
            else:
                threading.Thread(target=self._bot_action, args=("start", name), daemon=True).start()

        elif action == "list":
            log.info("[CMD] 봇 목록:")
            for b in BOTS:
                enabled = "활성" if b.get("enabled", True) else "비활성"
                log.info(f"  • {b['name']} ({b['script']}) — {enabled}")

        elif action in ("netcheck", "network"):
            self._netcheck_now()

        elif action == "help":
            log.info("=" * 40)
            log.info("[ Watchdog 명령어 ]")
            log.info("  status                — 봇 상태 확인")
            log.info("  restart [봇이름|all]  — 재시작")
            log.info("  stop    [봇이름|all]  — 중지")
            log.info("  start   [봇이름|all]  — 시작")
            log.info("  list                  — 봇 목록")
            log.info("  netcheck              — 네트워크 확인")
            log.info("  quit                  — Watchdog 종료")
            log.info("[ 봇 IPC 명령어 ]  (! 있어도 없어도 됨)")
            log.info("  boss    [명령어] [인자...]  — Boss Bot에 전달")
            log.info("  예) boss history 7  /  boss listdailyboss")
            log.info("  명령어 목록은 봇 행의 📋 버튼으로 확인")
            log.info("=" * 40)

        elif action in ("quit", "exit"):
            self._on_close()

        else:
            log.info(f"[CMD] 알 수 없는 명령어: {raw}  (help 로 도움말 확인)")

    # ── IPC 호출 (별도 스레드에서 실행) ────────────────────────────────────
    def _ipc_call(self, bot_key: str, sub_cmd: str, sub_args: list):
        log.info(f"[IPC → {bot_key}] {sub_cmd} {' '.join(sub_args)}")
        try:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(_send_ipc(bot_key, sub_cmd, sub_args))
            loop.close()
        except Exception as e:
            log.info(f"[IPC] 오류: {e}")
            return

        if not result.get("ok"):
            log.info(f"[IPC ← {bot_key}] ❌ {result.get('error', '오류')}")
            return

        messages = result.get("messages", [])
        if not messages:
            log.info(f"[IPC ← {bot_key}] ✅ 완료")
            return
        for msg in messages:
            if not msg:
                continue
            for line in msg.splitlines():
                log.info(f"[IPC ← {bot_key}] {line}")

    def _cmd_status(self):
        with bot_states_lock:
            states_copy = {k: dict(v) for k, v in bot_states.items()}
        log.info("=" * 40)
        log.info("[STATUS] 봇 상태:")
        for b in BOTS:
            name  = b["name"]
            state = states_copy.get(name, {})
            process = state.get("process")
            stopped = state.get("stopped", True)
            if stopped:
                status = "🔴 중지"
            elif process and process.poll() is None:
                status = f"🟢 실행중 (PID: {process.pid})"
            else:
                status = "🟡 재시작 대기"
            log.info(f"  {name}: {status}")
        log.info("=" * 40)

    def _cmd_history_prev(self):
        if not self._cmd_history:
            return
        if self._cmd_history_idx == -1:
            self._cmd_history_idx = len(self._cmd_history) - 1
        elif self._cmd_history_idx > 0:
            self._cmd_history_idx -= 1
        self._cmd_var.set(self._cmd_history[self._cmd_history_idx])
        self._cmd_entry.icursor("end")

    def _cmd_history_next(self):
        if self._cmd_history_idx == -1:
            return
        if self._cmd_history_idx < len(self._cmd_history) - 1:
            self._cmd_history_idx += 1
            self._cmd_var.set(self._cmd_history[self._cmd_history_idx])
        else:
            self._cmd_history_idx = -1
            self._cmd_var.set("")
        self._cmd_entry.icursor("end")

    # ── 봇 상세 로그 팝업 ─────────────────────────────────────────────────
    def _open_bot_log(self, bot_name: str):
        attr = f"_bot_log_win_{bot_name.replace(' ', '_')}"
        existing = getattr(self, attr, None)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self)
        win.title(f"📄 {bot_name} 상세 로그")
        win.configure(bg=_C["bg"])
        win.minsize(700, 420)
        win.resizable(True, True)
        setattr(self, attr, win)
        self._center_on_parent(win)

        # ── 헤더 ──
        hdr = tk.Frame(win, bg=_C["panel"], pady=5)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"📄  {bot_name} 상세 로그",
                 font=self._font_body, bg=_C["panel"], fg=_C["yellow"], padx=12).pack(side="left")

        # 자동 스크롤 토글
        autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(hdr, text="자동 스크롤", variable=autoscroll_var,
                       font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                       selectcolor=_C["bg"], activebackground=_C["panel"],
                       relief="flat").pack(side="right", padx=8)

        # 지우기 버튼
        def _clear():
            with bot_log_lock:
                bot_log_lines[bot_name].clear()
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.configure(state="disabled")

        tk.Button(hdr, text="🗑 지우기", command=_clear,
                  font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                  relief="flat", padx=8, pady=2, cursor="hand2").pack(side="right", padx=4)

        # 필터 검색
        filter_var = tk.StringVar()
        filter_frame = tk.Frame(win, bg=_C["bg"])
        filter_frame.pack(fill="x", padx=8, pady=(5, 2))
        tk.Label(filter_frame, text="🔍 필터:", font=self._font_small,
                 bg=_C["bg"], fg=_C["text_dim"]).pack(side="left", padx=(0, 4))
        filter_entry = tk.Entry(filter_frame, textvariable=filter_var,
                                font=self._font_mono, bg=_C["log_bg"], fg=_C["text"],
                                insertbackground=_C["text"], relief="flat", bd=0)
        filter_entry.pack(side="left", fill="x", expand=True, ipady=3)
        filter_entry.focus_set()

        # ── 로그 텍스트 ──
        log_frame = tk.Frame(win, bg=_C["bg"])
        log_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        sb = ttk.Scrollbar(log_frame, orient="vertical")
        sb.pack(side="right", fill="y")

        txt = tk.Text(
            log_frame, font=self._font_mono,
            bg=_C["log_bg"], fg=_C["log_text"],
            insertbackground=_C["text"],
            selectbackground=_C["border"],
            relief="flat", bd=0, wrap="word",
            yscrollcommand=sb.set, state="disabled",
        )
        txt.pack(fill="both", expand=True)
        sb.configure(command=txt.yview)

        # 태그
        txt.tag_config("time",  foreground=_C["log_time"])
        txt.tag_config("err",   foreground=_C["log_err"])
        txt.tag_config("warn",  foreground=_C["log_warn"])
        txt.tag_config("ok",    foreground=_C["green"])
        txt.tag_config("hi",    background="#3e3e5e")   # 필터 하이라이트

        # 기존 버퍼 내용 로드
        with bot_log_lock:
            history = list(bot_log_lines[bot_name])

        def _insert_line(line: str, tag_extra=None):
            txt.configure(state="normal")
            if line.startswith("[") and "]" in line:
                end = line.index("]") + 1
                txt.insert("end", line[:end], "time")
                rest = line[end:]
            else:
                rest = line
                end  = 0
            tag = "ok" if any(k in rest for k in ("완료", "OK", "✅", "시작", "연결")) \
                else "err"  if any(k in rest for k in ("오류", "실패", "ERROR", "❌")) \
                else "warn" if any(k in rest for k in ("경고", "WARN", "재시작 대기")) \
                else ""
            txt.insert("end", rest + "\n", tag if not tag_extra else (tag, tag_extra))
            txt.configure(state="disabled")

        def _load_history(filter_text=""):
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.configure(state="disabled")
            ft = filter_text.lower()
            for ln in history:
                if ft and ft not in ln.lower():
                    continue
                _insert_line(ln)
            if autoscroll_var.get():
                txt.see("end")

        _load_history()

        # 필터 적용
        def _on_filter(*_):
            _load_history(filter_var.get())

        filter_var.trace_add("write", _on_filter)

        # 실시간 새 로그 폴링
        q = bot_log_queues[bot_name]

        def _poll_new():
            if not win.winfo_exists():
                return
            ft = filter_var.get().lower()
            count = 0
            while count < 100:
                try:
                    line = q.get_nowait()
                except queue.Empty:
                    break
                # 버퍼에도 저장 (이미 _bot_log에서 했지만 팝업 열린 이후 것만 추가)
                if not ft or ft in line.lower():
                    _insert_line(line)
                    if autoscroll_var.get():
                        txt.see("end")
                count += 1
            win.after(200, _poll_new)

        win.after(200, _poll_new)

    # ── 봇별 명령어 팝업 ───────────────────────────────────────────────────
    def _open_cmd_panel(self, bot_name: str):
        prefix = _BOT_NAME_TO_PREFIX.get(bot_name)
        if not prefix:
            return
        cmds = _BOT_COMMANDS.get(prefix, [])

        # 이미 열려 있으면 포커스만
        attr = f"_cmd_panel_{prefix}"
        existing = getattr(self, attr, None)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self)
        win.title(f"{bot_name} 명령어")
        win.configure(bg=_C["bg"])
        win.resizable(False, True)
        win.minsize(480, 300)
        setattr(self, attr, win)
        self._center_on_parent(win)

        # ── 헤더 ──
        hdr = tk.Frame(win, bg=_C["panel"], pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"📋  {bot_name} 명령어",
                 font=self._font_body, bg=_C["panel"], fg=_C["blue"],
                 padx=12).pack(side="left")
        tk.Label(hdr, text="클릭 → 입력창에 삽입  /  더블클릭 → 즉시 실행",
                 font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                 padx=8).pack(side="right")

        # ── 검색창 ──
        search_var = tk.StringVar()
        search_frame = tk.Frame(win, bg=_C["bg"])
        search_frame.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(search_frame, text="🔍", bg=_C["bg"], fg=_C["text_dim"],
                 font=self._font_small).pack(side="left", padx=(0, 4))
        search_entry = tk.Entry(search_frame, textvariable=search_var,
                                font=self._font_mono, bg=_C["log_bg"], fg=_C["text"],
                                insertbackground=_C["text"], relief="flat", bd=0)
        search_entry.pack(side="left", fill="x", expand=True, ipady=3)
        search_entry.focus_set()

        # ── 명령어 리스트 ──
        list_frame = tk.Frame(win, bg=_C["bg"])
        list_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        listbox = tk.Listbox(
            list_frame,
            font=self._font_mono,
            bg=_C["log_bg"], fg=_C["text"],
            selectbackground=_C["btn_blue"], selectforeground="white",
            activestyle="none",
            relief="flat", bd=0,
            yscrollcommand=scrollbar.set,
        )
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=listbox.yview)

        # 전체 항목 저장 (검색 필터용)
        all_items = [(f"!{prefix} {cmd}", desc) for cmd, desc in cmds]

        def _populate(filter_text=""):
            listbox.delete(0, "end")
            ft = filter_text.lower()
            for full_cmd, desc in all_items:
                if ft and ft not in full_cmd.lower() and ft not in desc.lower():
                    continue
                display = f"  {full_cmd:<42}  {desc}"
                listbox.insert("end", display)

        _populate()

        def _on_search(*_):
            _populate(search_var.get())

        search_var.trace_add("write", _on_search)

        def _get_selected_cmd():
            sel = listbox.curselection()
            if not sel:
                return None
            display = listbox.get(sel[0]).strip()
            # display 앞부분이 실제 명령어 (공백 기준 첫 42자 안)
            return display.split("  ")[0].strip()

        def _insert_cmd(evt=None):
            cmd = _get_selected_cmd()
            if not cmd:
                return
            self._cmd_var.set(cmd)
            self._cmd_entry.focus_set()
            self._cmd_entry.icursor("end")

        def _run_cmd_now(evt=None):
            cmd = _get_selected_cmd()
            if not cmd:
                return
            self._cmd_var.set(cmd)
            self._cmd_entry.focus_set()
            self._run_cmd()

        listbox.bind("<<ListboxSelect>>", _insert_cmd)
        listbox.bind("<Double-Button-1>",  _run_cmd_now)
        search_entry.bind("<Return>",      _run_cmd_now)
        search_entry.bind("<Down>",        lambda e: listbox.focus_set() or listbox.selection_set(0))

        # ── 하단 버튼 ──
        btn_row = tk.Frame(win, bg=_C["bg"])
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(btn_row, text="✏ 입력창에 삽입", command=_insert_cmd,
                  font=self._font_small, bg=_C["btn_blue"], fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="▶ 즉시 실행", command=_run_cmd_now,
                  font=self._font_small, bg=_C["btn_green"], fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="닫기", command=win.destroy,
                  font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                  relief="flat", padx=10, pady=3, cursor="hand2").pack(side="right")

    # ── 봇 제어 ────────────────────────────────────────────────────────────
    def _bot_action(self, action: str, name: str):
        self._status_var.set(f"{name} {action} 처리 중...")
        fn = {"start": start_bot, "stop": stop_bot, "restart": restart_bot}[action]
        threading.Thread(target=fn, args=(name,), daemon=True).start()

    def _all_action(self, action: str):
        self._status_var.set(f"전체 {action} 처리 중...")
        def _do():
            for b in BOTS:
                if action == "start":
                    if b.get("enabled", True):
                        start_bot(b["name"])
                elif action == "stop":
                    stop_bot(b["name"])
                elif action == "restart":
                    restart_bot(b["name"])
        threading.Thread(target=_do, daemon=True).start()

    # ── 네트워크 수동 확인 ─────────────────────────────────────────────────
    def _netcheck_now(self):
        self._net_checked_lbl.configure(text="확인 중...")
        def _do():
            results = run_network_checks()
            log_network_results("수동 체크", results)
        threading.Thread(target=_do, daemon=True).start()

    # ── 봇 enabled 토글 ────────────────────────────────────────────────────
    def _toggle_bot_enabled(self, name: str, var: tk.BooleanVar):
        enabled = var.get()
        with bot_states_lock:
            if name in bot_states:
                bot_states[name]["enabled"] = enabled
        log.info(f"[설정] {name} 자동 재시작 {'활성화' if enabled else '비활성화'}")

    # ── 시스템 트레이 ──────────────────────────────────────────────────────
    def _make_tray_icon_image(self):
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Images", "Icon", "WatchDog.png")
        if os.path.exists(_icon_path):
            try:
                return Image.open(_icon_path).convert("RGBA").resize((64, 64))
            except Exception:
                pass
        img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill="#1e66f5")
        draw.text((18, 16), "W", fill="white")
        return img

    def _setup_tray(self):
        try:
            img  = self._make_tray_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("열기",   lambda: self.after(0, self._show_window), default=True),
                pystray.MenuItem("상태",   lambda: self.after(0, self._tray_status_notify)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료",   lambda: self.after(0, self._on_close)),
            )
            self._tray_icon = pystray.Icon("RaidBook Watchdog", img, "RaidBook Watchdog", menu)
            t = threading.Thread(target=self._tray_icon.run, daemon=True)
            t.start()
        except Exception as e:
            log.info(f"[트레이] 초기화 실패: {e}")
            self._tray_icon = None

    def _tray_status_notify(self):
        with bot_states_lock:
            states_copy = {k: dict(v) for k, v in bot_states.items()}
        lines = []
        for b in BOTS:
            state = states_copy.get(b["name"], {})
            if state.get("rapid_limit"):
                icon = "⚠"
            elif state.get("stopped", True):
                icon = "🔴"
            elif state.get("process") and state["process"].poll() is None:
                icon = "🟢"
            else:
                icon = "🟡"
            lines.append(f"{icon} {b['name']}")
        self._dispatch_notify("RaidBook Watchdog 상태", "\n".join(lines))

    @staticmethod
    def _virtual_screen_bounds() -> tuple[int, int, int, int]:
        """멀티 모니터 전체 가상 스크린의 (x, y, width, height) 반환.

        Windows API(SM_XVIRTUALSCREEN 등)로 읽고, 실패 시 주모니터 기준으로 폴백한다.
        """
        try:
            import ctypes
            u32 = ctypes.windll.user32
            vx = u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            vy = u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            vw = u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
            vh = u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
            if vw > 0 and vh > 0:
                return vx, vy, vw, vh
        except Exception:
            pass
        # 폴백: tkinter 주모니터 크기
        import tkinter as _tk
        _r = _tk.Tk()
        _r.withdraw()
        w, h = _r.winfo_screenwidth(), _r.winfo_screenheight()
        _r.destroy()
        return 0, 0, w, h

    def _center_on_parent(self, win: tk.Toplevel):
        """팝업 창을 메인 Watchdog 창 중앙에 위치시킨다.

        <Map> 이벤트(창이 실제로 화면에 그려진 직후)에 실행.
        가상 스크린 전체 범위로 클램프하므로 보조 모니터에서도 정상 동작한다.
        """
        def _do_center(_evt=None):
            # 부모·팝업 위치/크기를 OS와 동기화
            self.update_idletasks()
            win.update_idletasks()
            # 팝업 실제 크기 (렌더 전이면 요청 크기로 폴백)
            pw = win.winfo_width()
            ph = win.winfo_height()
            if pw <= 1:
                pw = win.winfo_reqwidth()
                ph = win.winfo_reqheight()
            mx = self.winfo_x()
            my = self.winfo_y()
            mw = self.winfo_width()
            mh = self.winfo_height()
            x = mx + (mw - pw) // 2
            y = my + (mh - ph) // 2
            # 가상 스크린 전체 범위로 클램프 (멀티 모니터 지원)
            vx, vy, vw, vh = WatchdogApp._virtual_screen_bounds()
            x = max(vx, min(x, vx + vw - pw))
            y = max(vy, min(y, vy + vh - ph))
            win.geometry(f"+{x}+{y}")

        # 창이 화면에 실제로 나타난 직후 한 번만 실행
        win.bind("<Map>", lambda e: (_do_center(), win.unbind("<Map>")), add="+")

    def _minimize_to_tray(self):
        self.withdraw()
        if self._tray_icon:
            try:
                self._tray_icon.notify("RaidBook Watchdog가 트레이로 최소화됐습니다.", "RaidBook Watchdog")
            except Exception:
                pass

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_unmap(self, event):
        """창 최소화(iconify) 시 트레이로 보냄."""
        if event.widget is self and _TRAY_AVAILABLE and self._tray_icon:
            self.after(50, self.withdraw)

    # ── 설정 팝업 ──────────────────────────────────────────────────────────
    def _open_settings(self):
        if hasattr(self, "_settings_win") and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("⚙ Watchdog 설정")
        win.configure(bg=_C["bg"])
        win.resizable(False, False)
        win.grab_set()           # 모달
        self._settings_win = win
        self._center_on_parent(win)

        # ── 헬퍼 ──────────────────────────────────────────────────────────
        def _section(text):
            f = tk.Frame(win, bg=_C["bg"])
            f.pack(fill="x", padx=16, pady=(14, 2))
            tk.Label(f, text=text, font=self._font_small,
                     bg=_C["bg"], fg=_C["blue"]).pack(side="left")
            tk.Frame(f, bg=_C["border"], height=1).pack(side="left", fill="x", expand=True, padx=(6, 0))

        def _row(parent, label, tip=""):
            fr = tk.Frame(parent, bg=_C["panel"])
            fr.pack(fill="x", padx=16, pady=2)
            lbl = tk.Label(fr, text=label, font=self._font_small,
                           bg=_C["panel"], fg=_C["text"], width=26, anchor="w")
            lbl.pack(side="left", padx=(10, 6), pady=5)
            if tip:
                _Tooltip(lbl, tip)
            return fr

        def _int_entry(parent, val: int, min_v: int, max_v: int):
            var = tk.StringVar(value=str(val))
            ent = tk.Entry(parent, textvariable=var, font=self._font_mono,
                           bg=_C["log_bg"], fg=_C["text"],
                           insertbackground=_C["text"],
                           relief="flat", bd=0, width=8)
            ent.pack(side="left", ipady=3, padx=(0, 4))
            tk.Label(parent, text=f"({min_v}–{max_v})", font=self._font_small,
                     bg=_C["panel"], fg=_C["text_dim"]).pack(side="left")
            return var

        def _bool_check(parent, val: bool, label="사용"):
            var = tk.BooleanVar(value=val)
            tk.Checkbutton(parent, text=label, variable=var,
                           font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                           selectcolor=_C["bg"], activebackground=_C["panel"],
                           relief="flat").pack(side="left", padx=4)
            return var

        # ── 섹션 1: 재시작 정책 ────────────────────────────────────────────
        _section("재시작 정책")

        r1 = _row(win, "재시작 대기 시간 (초)",
                  "봇이 종료된 후 재시작까지 기다리는 초.\n값이 클수록 연속 크래시 시 부하가 줄어듭니다.")
        v_delay = _int_entry(r1, RESTART_DELAY, 1, 300)

        r2 = _row(win, "빠른 재시작 최대 횟수",
                  "이 횟수 이상 빠르게 재시작하면 감시를 중단합니다.")
        v_max_restart = _int_entry(r2, MAX_RAPID_RESTARTS, 1, 20)

        r3 = _row(win, "빠른 재시작 감지 윈도우 (초)",
                  "이 시간(초) 안에 최대 횟수 이상 재시작하면 중단합니다.")
        v_rapid_window = _int_entry(r3, RAPID_RESTART_WINDOW, 10, 3600)

        # ── 섹션 2: Discord 봇 연결 ───────────────────────────────────────
        _section("Discord 봇 연결")

        # 안내 문구
        notice_frame = tk.Frame(win, bg="#2a2a1a")
        notice_frame.pack(fill="x", padx=16, pady=(2, 4))
        tk.Label(notice_frame, text="⚠  변경 시 Discord 봇이 자동으로 재연결됩니다.",
                 font=self._font_small, bg="#2a2a1a", fg=_C["yellow"],
                 anchor="w", padx=10, pady=4).pack(fill="x")

        def _str_entry(parent, val: str, show=""):
            var = tk.StringVar(value=val)
            ent = tk.Entry(parent, textvariable=var, font=self._font_mono,
                           bg=_C["log_bg"], fg=_C["text"],
                           insertbackground=_C["text"],
                           relief="flat", bd=0, width=36, show=show)
            ent.pack(side="left", ipady=3, padx=(0, 4))
            return var

        r7 = _row(win, "Bot Token",
                  "Discord 개발자 포털에서 발급한 봇 토큰.\n눈 아이콘으로 표시/숨김 전환 가능합니다.")
        v_token = _str_entry(r7, WATCHDOG_TOKEN, show="•")

        # 토큰 표시/숨김 토글
        token_visible = tk.BooleanVar(value=False)
        def _toggle_token():
            ent = r7.winfo_children()[1]   # Entry 위젯
            ent.configure(show="" if token_visible.get() else "•")
        tk.Checkbutton(r7, text="👁", variable=token_visible, command=_toggle_token,
                       font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                       selectcolor=_C["bg"], activebackground=_C["panel"],
                       relief="flat", cursor="hand2").pack(side="left", padx=4)


        # ── 섹션 3: 봇 필수 설정 (Watchdog/Boss) ───────────────────────────
        _section("봇 설정 (Watchdog / Boss)")

        r9 = _row(win, "봇 토큰 설정",
                  "설정 마법사로 각 봇의 토큰을 확인·수정합니다. 채널은 자동 생성됩니다.\n"
                  "저장하면 봇이 자동 재시작되어 즉시 적용됩니다.")

        def _open_wizard_from_settings():
            win.destroy()                      # 모달 grab 충돌 방지 — 설정 창 닫고 마법사 열기
            self._open_config_wizard()

        wiz_btn = tk.Button(r9, text="🧭 설정 마법사 열기", command=_open_wizard_from_settings,
                            font=self._font_small, bg=_C["btn_blue"], fg="white",
                            relief="flat", padx=10, pady=3, cursor="hand2")
        wiz_btn.pack(side="left")

        # ── 섹션 4: 백업 / 이동 ───────────────────────────────────────────
        _section("백업 / 이동")

        r10 = _row(win, "설정·데이터 백업",
                   "Config와 Data(롤링 백업 제외)를 ZIP 파일 하나로 저장합니다.\n"
                   "다른 PC로 이전하거나 복원용 백업을 만들 때 사용하세요.")
        tk.Button(r10, text="📤 내보내기", command=lambda: self._export_settings_dialog(parent=win),
                  font=self._font_small, bg=_C["btn_green"], fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left")

        r11 = _row(win, "설정·데이터 복원",
                   "백업 ZIP 또는 Config/Data가 들어 있는 폴더에서 복원합니다.\n"
                   "적용을 위해 모든 봇이 자동 재시작됩니다.")

        def _import_from_settings():
            win.destroy()                  # 모달 grab 충돌 방지 — 설정 창 닫고 진행
            self._import_settings_dialog()

        tk.Button(r11, text="📥 가져오기", command=_import_from_settings,
                  font=self._font_small, bg=_C["btn_yellow"], fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left")

        # ── 구분 ──────────────────────────────────────────────────────────
        tk.Frame(win, bg=_C["border"], height=1).pack(fill="x", padx=16, pady=(14, 0))

        # ── 결과 메시지 ───────────────────────────────────────────────────
        result_var = tk.StringVar()
        result_lbl = tk.Label(win, textvariable=result_var, font=self._font_small,
                              bg=_C["bg"], fg=_C["green"], anchor="w")
        result_lbl.pack(fill="x", padx=18, pady=(6, 0))

        # ── 적용 버튼 ─────────────────────────────────────────────────────
        def _apply():
            global RESTART_DELAY, MAX_RAPID_RESTARTS, RAPID_RESTART_WINDOW
            global WATCHDOG_TOKEN

            errors = []

            def _parse_int(var, name, min_v, max_v):
                try:
                    val = int(var.get())
                    if not (min_v <= val <= max_v):
                        raise ValueError
                    return val
                except ValueError:
                    errors.append(f"{name}: {min_v}~{max_v} 사이 정수를 입력하세요.")
                    return None

            delay        = _parse_int(v_delay,       "재시작 대기",        1,  300)
            max_r        = _parse_int(v_max_restart,  "빠른 재시작 횟수",   1,   20)
            rapid_w      = _parse_int(v_rapid_window, "빠른 재시작 윈도우", 10, 3600)

            new_token   = v_token.get().strip()

            if errors:
                result_var.set("❌ " + " / ".join(errors))
                result_lbl.configure(fg=_C["red"])
                return

            # ── 즉시 적용 ─────────────────────────────────────────────────
            # run_bot/_wdog_bot 등 실제 동작 로직은 공유 코어(RaidBook_Watchdog)
            # 모듈에 있으므로, 그 모듈의 전역도 함께 갱신해야 즉시 적용된다.
            RESTART_DELAY          = delay
            MAX_RAPID_RESTARTS     = max_r
            RAPID_RESTART_WINDOW   = rapid_w
            _core.RESTART_DELAY          = RESTART_DELAY
            _core.MAX_RAPID_RESTARTS     = MAX_RAPID_RESTARTS
            _core.RAPID_RESTART_WINDOW   = RAPID_RESTART_WINDOW

            # Discord 봇 재연결 필요 여부
            discord_changed = new_token != WATCHDOG_TOKEN
            WATCHDOG_TOKEN   = new_token
            _core.WATCHDOG_TOKEN      = WATCHDOG_TOKEN

            # config 파일에 저장
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["restart_delay_seconds"]        = RESTART_DELAY
                cfg["max_rapid_restarts"]           = MAX_RAPID_RESTARTS
                cfg["rapid_restart_window_seconds"] = RAPID_RESTART_WINDOW
                cfg["token"]                        = WATCHDOG_TOKEN
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                log.info("[설정] 변경 사항 적용 및 파일 저장 완료")

                if discord_changed:
                    result_var.set("✅ 저장 완료 — Discord 봇 재연결 중...")
                    result_lbl.configure(fg=_C["yellow"])
                    win.after(500, lambda: threading.Thread(
                        target=_restart_discord_bot, daemon=True).start())
                    win.after(3000, lambda: result_var.set(
                        "✅ Discord 봇 재연결 완료 (연결 상태는 로그 확인)"))
                else:
                    result_var.set("✅ 적용 완료 — config 파일에 저장됐습니다.")
                result_lbl.configure(fg=_C["green"])
            except Exception as e:
                log.info(f"[설정] 파일 저장 실패: {e}")
                result_var.set(f"⚠ 메모리 적용은 됐으나 파일 저장 실패: {e}")
                result_lbl.configure(fg=_C["yellow"])

        btn_row = tk.Frame(win, bg=_C["bg"])
        btn_row.pack(fill="x", padx=16, pady=(6, 16))

        tk.Button(btn_row, text="✅ 적용", command=_apply,
                  font=self._font_small, bg=_C["btn_green"], fg="white",
                  relief="flat", padx=14, pady=4, cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="닫기", command=win.destroy,
                  font=self._font_small, bg=_C["panel"], fg=_C["text_dim"],
                  relief="flat", padx=14, pady=4, cursor="hand2").pack(side="left")

        # Enter → 적용
        win.bind("<Return>", lambda e: _apply())
        win.bind("<Escape>", lambda e: win.destroy())

    # ── 초기화 완료 (일반 실행 또는 마법사 완료 후 호출) ─────────────────────
    def _finish_init(self):
        """UI 빌드 + 트레이 + 폴링 시작. 마법사 완료 후에도 호출된다."""
        global _WIZARD_MODE

        self._build_ui()

        if _TRAY_AVAILABLE:
            self._setup_tray()

        self._schedule_status_poll()
        self._schedule_log_poll()
        self._schedule_notify_poll()

        # 시작 5초 뒤 자동 업데이트 체크 (조용히 — 새 버전 있을 때만 알림)
        if _UPDATER_AVAILABLE and version:
            self.after(5000, self._auto_check_updates)

        self.deiconify()
        self.lift()
        self.focus_force()

        # 마법사 완료 후 Discord 봇 + 서브봇 지연 시작
        if _WIZARD_MODE:
            _WIZARD_MODE = False
            log.info("[마법사] 설정 완료 — Discord 봇 및 서브봇 시작")
            start_discord_bot_thread()
            start_all_enabled_bots()

    # ── 설정·데이터 내보내기 / 가져오기 ─────────────────────────────────────
    def _reload_watchdog_globals(self):
        """watchdog_config.json을 다시 읽어 GUI/코어 전역에 반영."""
        global WATCHDOG_TOKEN, ADMIN_CHANNEL_ID
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            log.info(f"[가져오기] watchdog config 재로드 실패: {e}")
            return
        WATCHDOG_TOKEN   = cfg.get("token", "") or ""
        ADMIN_CHANNEL_ID = int(cfg.get("admin_channel_id") or 0)
        _core.WATCHDOG_TOKEN      = WATCHDOG_TOKEN
        _core.ADMIN_CHANNEL_ID    = ADMIN_CHANNEL_ID

    def _export_settings_dialog(self, parent=None):
        parent = parent or self
        ts = datetime.now().strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            parent=parent, title="설정·데이터 내보내기",
            defaultextension=".zip", initialfile=f"RaidBook_backup_{ts}.zip",
            filetypes=[("ZIP 파일", "*.zip")])
        if not path:
            return
        try:
            n = export_settings(path)
            log.info(f"[백업] 내보내기 완료: {path} ({n}개 파일)")
            messagebox.showinfo("내보내기 완료",
                                f"{n}개 파일을 내보냈습니다.\n\n{path}", parent=parent)
        except Exception as e:
            log.info(f"[백업] 내보내기 실패: {e}")
            messagebox.showerror("내보내기 실패", str(e), parent=parent)

    def _ask_import_source(self, parent) -> str:
        """가져올 원본(ZIP 파일 또는 폴더) 선택. 취소 시 빈 문자열."""
        sel = {"path": ""}
        dlg = tk.Toplevel(parent)
        dlg.title("가져오기 원본 선택")
        dlg.configure(bg=_C["bg"])
        dlg.resizable(False, False)
        dlg.transient(parent)
        dlg.grab_set()

        tk.Label(dlg, text="가져올 원본의 형태를 선택하세요.",
                 font=self._font_body, bg=_C["bg"], fg=_C["text"],
                 padx=24, pady=14).pack()
        tk.Label(dlg, text="ZIP: 내보내기로 만든 백업 파일\n"
                           "폴더: Config/Data가 들어 있는 폴더 (예전 설치 위치 등)",
                 font=self._font_small, bg=_C["bg"], fg=_C["text_dim"],
                 justify="left", padx=24).pack()

        btns = tk.Frame(dlg, bg=_C["bg"])
        btns.pack(padx=24, pady=14)

        def _pick_zip():
            p = filedialog.askopenfilename(parent=dlg, title="백업 ZIP 선택",
                                           filetypes=[("ZIP 파일", "*.zip")])
            if p:
                sel["path"] = p
                dlg.destroy()

        def _pick_dir():
            p = filedialog.askdirectory(parent=dlg, title="Config/Data가 들어 있는 폴더 선택")
            if p:
                sel["path"] = p
                dlg.destroy()

        for text, cmd, color in [("📦 ZIP 파일", _pick_zip, _C["btn_blue"]),
                                 ("📁 폴더", _pick_dir, _C["btn_green"]),
                                 ("취소", dlg.destroy, _C["panel"])]:
            tk.Button(btns, text=text, command=cmd, font=self._font_small,
                      bg=color, fg="white" if color != _C["panel"] else _C["text_dim"],
                      relief="flat", padx=14, pady=6, cursor="hand2").pack(side="left", padx=5)

        self._center_on_parent(dlg)
        dlg.wait_window()
        return sel["path"]

    def _import_settings_dialog(self, parent=None, restart_after=True) -> bool:
        """설정·데이터 가져오기. restart_after=True면 적용을 위해 봇 재시작."""
        parent = parent or self
        src = self._ask_import_source(parent)
        if not src:
            return False

        if not messagebox.askyesno(
                "가져오기 확인",
                "현재 설정과 데이터를 가져온 내용으로 덮어씁니다.\n"
                "덮어쓰기 전 현재 상태는 Backup 폴더에 자동 백업됩니다.\n\n"
                + ("적용을 위해 모든 봇이 재시작됩니다. 계속할까요?"
                   if restart_after else "계속할까요?"),
                parent=parent):
            return False

        try:
            if restart_after:
                # 파일 덮어쓰기 전 봇 중지 (Excel/JSON 파일 잠금 회피)
                for b in BOTS:
                    stop_bot(b["name"])
                time.sleep(1.0)

            n = import_settings(src)
            self._reload_watchdog_globals()
            log.info(f"[가져오기] 완료: {src} ({n}개 파일)")

            if restart_after:
                threading.Thread(target=_restart_discord_bot, daemon=True).start()
                self._all_action("start")

            # 필수값이 모두 채워졌으면 경고 배너 제거
            banner = getattr(self, "_config_banner", None)
            if banner and banner.winfo_exists() and not missing_required_configs():
                banner.destroy()
                self._config_banner = None

            messagebox.showinfo("가져오기 완료",
                                f"{n}개 파일을 적용했습니다."
                                + ("\n봇을 재시작했습니다." if restart_after else ""),
                                parent=parent)
            return True
        except Exception as e:
            log.info(f"[가져오기] 실패: {e}")
            messagebox.showerror("가져오기 실패", str(e), parent=parent)
            if restart_after:
                self._all_action("start")   # 실패해도 봇은 다시 올린다
            return False

    # ── 설정 마법사 열기 (실행 중 수정) ─────────────────────────────────────
    def _open_config_wizard(self):
        """실행 중 봇 토큰을 수정하는 마법사. 저장 시 봇 자동 재시작."""

        def _on_saved():
            # 필수값이 모두 채워졌으면 경고 배너 제거
            banner = getattr(self, "_config_banner", None)
            if banner and banner.winfo_exists() and not missing_required_configs():
                banner.destroy()
                self._config_banner = None
            log.info("[설정] 봇 설정 저장 완료 — 변경 적용을 위해 봇 재시작")
            _notify("RaidBook Watchdog", "설정 저장 완료 — 봇을 재시작합니다.")
            threading.Thread(target=_restart_discord_bot, daemon=True).start()
            self._all_action("restart")

        self._show_setup_wizard(on_complete=_on_saved, edit_mode=True)

    # ── 초기 설정 마법사 ─────────────────────────────────────────────────────
    def _show_setup_wizard(self, on_complete, edit_mode=False):
        """config 미설정 시 표시되는 3단계(Watchdog·Boss Bot·TTS Bot) 초기 설정 마법사. TTS 토큰은 선택.

        edit_mode=True 이면 실행 중 설정 수정 용도로 열린다:
        닫기(X/Esc)로 취소할 수 있고, 저장 시 on_complete가 봇을 재시작한다.
        """

        _BOSS_PATH   = get_config_path("boss_config.json")
        _TTS_PATH    = get_config_path("tts_config.json")

        for _path in (CONFIG_PATH, _BOSS_PATH, _TTS_PATH):
            ensure_config_from_template(_path)

        def _load(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}

        wdog_c   = _load(CONFIG_PATH)
        boss_c   = _load(_BOSS_PATH)
        tts_c    = _load(_TTS_PATH)

        # ── 창 ────────────────────────────────────────────────────────────────
        win = tk.Toplevel(self)
        win.title("RaidBook Watchdog — " + ("봇 설정 수정" if edit_mode else "초기 설정 마법사"))
        win.configure(bg=_C["bg"])
        win.resizable(False, False)
        if edit_mode:
            win.protocol("WM_DELETE_WINDOW", win.destroy)   # 수정 모드는 취소(닫기) 허용
            win.bind("<Escape>", lambda e: win.destroy())
            win.grab_set()
        else:
            # 첫 실행 마법사: 이 시점엔 메인 창도 트레이도 없으므로
            # X버튼이 유일한 출구 — 확인 후 프로그램 전체 종료
            def _confirm_quit():
                if messagebox.askyesno(
                        "RaidBook Watchdog",
                        "설정이 아직 완료되지 않았습니다.\n"
                        "설정을 중단하고 프로그램을 종료할까요?\n\n"
                        "(다음 실행 시 마법사가 다시 표시됩니다)",
                        parent=win):
                    self._on_close()
            win.protocol("WM_DELETE_WINDOW", _confirm_quit)

        W, H = 620, 560
        win.update_idletasks()
        sx, sy = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{W}x{H}+{(sx - W) // 2}+{(sy - H) // 2}")
        win.after(120, win.focus_force)

        # ── 헤더 ──────────────────────────────────────────────────────────────
        hdr = tk.Frame(win, bg="#1a2744", pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="\U0001f6e1  RaidBook Watchdog  —  " + ("봇 설정 수정" if edit_mode else "초기 설정 마법사"),
                 font=self._font_title, bg="#1a2744", fg=_C["blue"],
                 padx=20).pack(anchor="w")
        tk.Label(hdr, text=("각 봇의 토큰을 수정합니다. 채널은 자동 생성됩니다. 저장하면 봇이 자동 재시작됩니다."
                            if edit_mode else
                            "시스템 첫 실행 시 각 봇의 토큰을 설정합니다. 채널은 자동 생성됩니다."),
                 font=self._font_small, bg="#1a2744", fg=_C["text_dim"],
                 padx=20).pack(anchor="w")
        tk.Label(hdr, text="※ 토큰은 Discord 개발자 포털에서 발급하며, 각 봇의 Bot 탭에서 "
                           "Message Content Intent와 Server Members Intent를 켜야 합니다.",
                 font=self._font_small, bg="#1a2744", fg=_C["yellow"],
                 padx=20).pack(anchor="w", pady=(0, 4))

        # ── 스텝 진행 표시 ────────────────────────────────────────────────────
        STEP_META = [
            ("Watchdog",   "\U0001f6e1"),
            ("Boss Bot",   "⚔"),
            ("TTS Bot",    "\U0001f50a"),
        ]
        prog_frame = tk.Frame(win, bg=_C["panel"])
        prog_frame.pack(fill="x")
        _step_dots: list = []
        _step_lbls: list = []
        for sname, sicon in STEP_META:
            col = tk.Frame(prog_frame, bg=_C["panel"])
            col.pack(side="left", expand=True)
            d = tk.Label(col, text=sicon, font=("Segoe UI", 14),
                         bg=_C["panel"], fg=_C["text_dim"])
            d.pack(pady=(6, 0))
            l = tk.Label(col, text=sname, font=self._font_small,
                         bg=_C["panel"], fg=_C["text_dim"])
            l.pack(pady=(0, 6))
            _step_dots.append(d)
            _step_lbls.append(l)

        tk.Frame(win, bg=_C["border"], height=1).pack(fill="x")

        # ── 콘텐츠 영역 ───────────────────────────────────────────────────────
        content = tk.Frame(win, bg=_C["bg"])
        content.pack(fill="both", expand=True)

        # ── 하단 영역 ─────────────────────────────────────────────────────────
        tk.Frame(win, bg=_C["border"], height=1).pack(fill="x", side="bottom")
        bottom = tk.Frame(win, bg=_C["panel"], pady=8)
        bottom.pack(fill="x", side="bottom")

        result_var = tk.StringVar()
        result_lbl = tk.Label(bottom, textvariable=result_var,
                              font=self._font_small, bg=_C["panel"], fg=_C["red"],
                              anchor="w")
        result_lbl.pack(fill="x", padx=20, pady=(0, 4))

        nav = tk.Frame(bottom, bg=_C["panel"])
        nav.pack(fill="x", padx=20)

        btn_prev = tk.Button(nav, text="◄ 이전", font=self._font_small,
                             bg=_C["panel"], fg=_C["text_dim"],
                             relief="flat", padx=14, pady=4, cursor="hand2",
                             state="disabled")
        btn_prev.pack(side="left")

        step_lbl_nav = tk.Label(nav, text="1 / 2", font=self._font_small,
                                bg=_C["panel"], fg=_C["text_dim"])
        step_lbl_nav.pack(side="left", padx=12)

        btn_next = tk.Button(nav, text="다음 ►", font=self._font_small,
                             bg=_C["btn_blue"], fg="white",
                             relief="flat", padx=14, pady=4, cursor="hand2")
        btn_next.pack(side="right")

        # 첫 실행 모드: 기존 백업/설치본에서 설정을 통째로 가져와 마법사 생략
        if not edit_mode:
            def _wizard_import():
                if not self._import_settings_dialog(parent=win, restart_after=False):
                    return
                win.destroy()
                still = missing_required_configs()
                if still:
                    messagebox.showinfo(
                        "추가 설정 필요",
                        "가져오기는 끝났지만 아직 비어 있는 필수 값이 있습니다:\n- "
                        + "\n- ".join(still)
                        + "\n\n마법사를 다시 열어 나머지를 입력해 주세요.",
                        parent=self)
                    self._show_setup_wizard(on_complete, edit_mode=edit_mode)
                else:
                    on_complete()

            btn_import = tk.Button(nav, text="📥 기존 설정 가져오기", command=_wizard_import,
                                   font=self._font_small, bg=_C["panel"], fg=_C["text"],
                                   relief="flat", padx=10, pady=4, cursor="hand2")
            btn_import.pack(side="right", padx=(0, 10))
            _Tooltip(btn_import, "백업 ZIP이나 예전 설치본의 Config/Data 폴더에서\n"
                                 "설정과 데이터를 가져와 입력 과정을 건너뜁니다.")

        # ── StringVar 사전 ────────────────────────────────────────────────────
        def _sv(val):
            s = str(val) if val else ""
            return tk.StringVar(value=s)

        v = {
            "wdog_token":     _sv(wdog_c.get("token", "")),
            "boss_token":     _sv(boss_c.get("token", "")),
            "tts_token":      _sv(tts_c.get("token", "")),
        }

        # ── 위젯 헬퍼        }

        # ── 위젯 헬퍼 ─────────────────────────────────────────────────────────
        def _step_header(parent, icon, title, desc):
            tk.Label(parent, text=f"{icon}  {title}",
                     font=self._font_title, bg=_C["bg"], fg=_C["blue"]).pack(anchor="w", pady=(0, 3))
            tk.Label(parent, text=desc, font=self._font_small,
                     bg=_C["bg"], fg=_C["text_dim"],
                     wraplength=540, justify="left").pack(anchor="w", pady=(0, 8))
            tk.Frame(parent, bg=_C["border"], height=1).pack(fill="x", pady=(0, 10))

        def _field(parent, label, var, required=True, hide=False, tip=""):
            row = tk.Frame(parent, bg=_C["bg"])
            row.pack(fill="x", pady=4)
            mark = "  *" if required else "   "
            tk.Label(row, text=label + mark, font=self._font_small,
                     bg=_C["bg"], fg=_C["text"] if required else _C["text_dim"],
                     width=22, anchor="w").pack(side="left")
            inner = tk.Frame(row, bg=_C["log_bg"], bd=1, relief="groove")
            inner.pack(side="left", fill="x", expand=True)
            ent = tk.Entry(inner, textvariable=var, font=self._font_mono,
                           bg=_C["log_bg"], fg=_C["text"],
                           insertbackground=_C["text"],
                           relief="flat", bd=0,
                           show="•" if hide else "")
            ent.pack(side="left", fill="x", expand=True, ipady=5, padx=6)
            if hide:
                vis = tk.BooleanVar(value=False)
                def _toggle(_e=ent, _v=vis):
                    _e.configure(show="" if _v.get() else "•")
                tk.Checkbutton(inner, text="\U0001f441", variable=vis, command=_toggle,
                               font=self._font_small, bg=_C["log_bg"], fg=_C["text_dim"],
                               selectcolor=_C["log_bg"], activebackground=_C["log_bg"],
                               relief="flat", cursor="hand2").pack(side="right", padx=4)
            if tip:
                _Tooltip(ent, tip)
            return ent

        # ── 스텝 프레임 빌드 ─────────────────────────────────────────────────
        frames = []

        def _mk():
            f = tk.Frame(content, bg=_C["bg"], padx=28, pady=14)
            frames.append(f)
            return f

        # Step 0 — Watchdog
        s0 = _mk()
        _step_header(s0, "\U0001f6e1", "Watchdog Bot 설정",
                     "Watchdog 봇의 토큰만 입력하면 됩니다.\n"
                     "관리 채널(raidbook-관리)은 봇이 처음 켜질 때 자동으로 만듭니다.")
        _field(s0, "봇 토큰",       v["wdog_token"],   hide=True,
               tip="Discord 개발자 포털 → Bot 탭 → Token 복사")

        # Step 1 — Boss Bot
        s1 = _mk()
        _step_header(s1, "⚔", "Boss Bot 설정",
                     "Boss Bot의 토큰만 입력하면 됩니다.\n"
                     "보탐-알림·보탐-명령·전리품·경매장 채널은 봇이 시작할 때 RaidBook 카테고리에 자동으로 만듭니다.\n"
                     "만든 뒤 이름을 바꾸거나 옮겨도 그대로 동작합니다 (채널 ID 기준).")
        _field(s1, "봇 토큰",       v["boss_token"],  hide=True,
               tip="Boss Bot Discord 봇 토큰")

        # Step 2 — TTS Bot (선택)
        s2 = _mk()
        _step_header(s2, "\U0001f50a", "TTS Bot 설정 (선택)",
                     "채팅에 #내용 을 쓰면 음성채널에서 읽어 주는 봇입니다. 쓰지 않으면 비워 두세요.\n"
                     "서버에 따로 추가한 TTS 전용 봇의 토큰을 넣어야 합니다 (Boss Bot 토큰과 달라야 함).\n"
                     "TTS 봇의 Bot 탭에서 Message Content Intent를 꼭 켜 주세요. 'tts' 채널은 자동으로 만듭니다.")
        _field(s2, "봇 토큰",       v["tts_token"],  required=False, hide=True,
               tip="TTS 전용 Discord 봇 토큰 (선택)")

        # ── 스텝 전환 로직 ────────────────────────────────────────────────────
        cur = [0]

        def _show(idx):
            for f in frames:
                f.pack_forget()
            frames[idx].pack(fill="both", expand=True)
            cur[0] = idx

            for i in range(len(STEP_META)):
                if i < idx:
                    c = _C["green"]
                elif i == idx:
                    c = _C["blue"]
                else:
                    c = _C["text_dim"]
                _step_dots[i].configure(fg=c)
                _step_lbls[i].configure(fg=c)

            btn_prev.configure(
                state="normal" if idx > 0 else "disabled",
                fg=_C["text"] if idx > 0 else _C["text_dim"])

            is_last = (idx == len(frames) - 1)
            btn_next.configure(
                text="✅ 완료" if is_last else "다음 ►",
                bg=_C["btn_green"] if is_last else _C["btn_blue"])

            step_lbl_nav.configure(text=f"{idx + 1} / {len(frames)}")
            result_var.set("")

        def _validate(idx):
            errors = []

            def _tk(key, label):
                val = v[key].get().strip()
                if not val:
                    errors.append(f"[{label}] 토큰을 입력하세요.")
                elif len(val) < 40:
                    errors.append(f"[{label}] 토큰 형식이 올바르지 않습니다.")

            def _ch(key, label, req=True):
                val = v[key].get().strip()
                if not val:
                    if req:
                        errors.append(f"[{label}] 채널 ID를 입력하세요.")
                    return
                try:
                    int(val)
                except ValueError:
                    errors.append(f"[{label}] 숫자만 입력하세요.")

            if idx == 0:
                _tk("wdog_token",     "Watchdog 토큰")
            elif idx == 1:
                _tk("boss_token",     "Boss Bot 토큰")
            elif idx == 2:
                tts = v["tts_token"].get().strip()
                if tts:
                    _tk("tts_token",  "TTS Bot 토큰")
                    if tts in (v["boss_token"].get().strip(), v["wdog_token"].get().strip()):
                        errors.append("[TTS Bot 토큰] 다른 봇과 같은 토큰입니다. TTS 전용 봇의 토큰을 넣어 주세요.")
            return errors

        def _save_all():
            global WATCHDOG_TOKEN

            def _str(key):
                return v[key].get().strip()

            def _int(key):
                s = v[key].get().strip()
                return int(s) if s else 0

            try:
                # watchdog_config.json
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        wdog = json.load(f)
                except Exception:
                    wdog = {}
                wdog["token"]            = _str("wdog_token")
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(wdog, f, ensure_ascii=False, indent=4)

                # boss_config.json
                try:
                    with open(_BOSS_PATH, "r", encoding="utf-8") as f:
                        boss = json.load(f)
                except Exception:
                    boss = {}
                boss["token"]              = _str("boss_token")
                with open(_BOSS_PATH, "w", encoding="utf-8") as f:
                    json.dump(boss, f, ensure_ascii=False, indent=4)

                # tts_config.json (선택 — 비어 있으면 TTS 봇은 대기만 함)
                tts = _load(_TTS_PATH)
                if tts.get("token", "") != _str("tts_token"):
                    tts["token"]           = _str("tts_token")
                    with open(_TTS_PATH, "w", encoding="utf-8") as f:
                        json.dump(tts, f, ensure_ascii=False, indent=4)

                # 전역 변수 즉시 반영 (run_bot/_wdog_bot 등은 공유 코어 모듈에 있으므로 그쪽도 갱신)
                WATCHDOG_TOKEN   = _str("wdog_token")
                _core.WATCHDOG_TOKEN      = WATCHDOG_TOKEN

                log.info("[마법사] 모든 설정 파일 저장 완료")
                return True

            except Exception as e:
                result_var.set(f"❌ 저장 실패: {e}")
                result_lbl.configure(fg=_C["red"])
                return False

        def _on_next():
            idx  = cur[0]
            errs = _validate(idx)
            if errs:
                result_var.set("❌ " + errs[0])
                result_lbl.configure(fg=_C["red"])
                return
            if idx < len(frames) - 1:
                _show(idx + 1)
            else:
                result_var.set("⏳ 저장 중...")
                result_lbl.configure(fg=_C["yellow"])
                win.update_idletasks()
                if _save_all():
                    win.destroy()
                    on_complete()

        def _on_prev():
            if cur[0] > 0:
                result_var.set("")
                _show(cur[0] - 1)

        btn_prev.configure(command=_on_prev)
        btn_next.configure(command=_on_next)
        win.bind("<Return>", lambda e: _on_next())

        _show(0)

    # ── 업데이트 자동 확인 (시작 시 + 6시간 주기, 조용히) ────────────────
    _AUTO_UPDATE_INTERVAL_MS = 6 * 3600 * 1000

    def _auto_check_updates(self):
        """백그라운드 업데이트 체크. 새 버전이 있을 때만 알림(버전당 1회)."""
        if not _UPDATER_AVAILABLE or not version or not updater:
            return

        def check_in_thread():
            try:
                checker = updater.UpdateChecker()
                has_update, release_info = checker.check_for_updates()
                if has_update and release_info:
                    self.after(0, lambda: self._notify_auto_update(release_info))
            except Exception as e:
                log.info(f"[업데이트] 자동 확인 실패 (무시): {e}")

        threading.Thread(target=check_in_thread, daemon=True).start()
        # 다음 주기 예약
        self.after(self._AUTO_UPDATE_INTERVAL_MS, self._auto_check_updates)

    def _notify_auto_update(self, release_info: dict):
        new_version = release_info.get("version", "")
        if getattr(self, "_notified_update_version", None) == new_version:
            return  # 이미 이 버전으로 알림함
        self._notified_update_version = new_version
        log.info(f"[업데이트] 새 버전 발견: v{new_version} (현재 v{version.VERSION})")
        # 트레이 토스트 + 업데이트 다이얼로그
        try:
            if self._tray_icon:
                self._tray_icon.notify(f"새 버전 v{new_version}이 출시되었습니다.", "RaidBook 업데이트")
            else:
                _win_toast("RaidBook 업데이트", f"새 버전 v{new_version}이 출시되었습니다.")
        except Exception:
            pass
        self._show_update_result({"has_update": True, "release_info": release_info, "error": None})

    # ── 업데이트 확인 ─────────────────────────────────────────────────────
    def _check_updates(self):
        """업데이트 확인 및 다운로드"""
        if not _UPDATER_AVAILABLE or not version or not updater:
            messagebox.showinfo("업데이트", "업데이트 기능을 사용할 수 없습니다.")
            return

        # 진행 다이얼로그
        progress_win = tk.Toplevel(self)
        progress_win.title("업데이트 확인")
        progress_win.configure(bg=_C["bg"])
        progress_win.resizable(False, False)
        progress_win.transient(self)
        progress_win.grab_set()

        # 메시지
        msg_frame = tk.Frame(progress_win, bg=_C["bg"])
        msg_frame.pack(fill="both", expand=True, padx=30, pady=20)

        status_var = tk.StringVar(value="⏳ 업데이트 확인 중...")
        status_lbl = tk.Label(msg_frame, textvariable=status_var,
                             font=self._font_body, bg=_C["bg"], fg=_C["text"])
        status_lbl.pack(pady=10)

        # 프로그레스 바
        progress_bar = ttk.Progressbar(msg_frame, mode='indeterminate', length=300)
        progress_bar.pack(pady=5)
        progress_bar.start(10)

        # 중앙 배치
        progress_win.update_idletasks()
        w = progress_win.winfo_reqwidth()
        h = progress_win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        progress_win.geometry(f"+{x}+{y}")

        result = {"has_update": False, "release_info": None, "error": None}

        def check_in_thread():
            """백그라운드에서 업데이트 확인"""
            try:
                checker = updater.UpdateChecker()
                has_update, release_info = checker.check_for_updates()
                result["has_update"] = has_update
                result["release_info"] = release_info
            except Exception as e:
                result["error"] = str(e)

        # 스레드 시작
        thread = threading.Thread(target=check_in_thread, daemon=True)
        thread.start()

        # 완료 대기
        def wait_complete():
            if thread.is_alive():
                self.after(100, wait_complete)
            else:
                progress_bar.stop()
                progress_win.destroy()
                self._show_update_result(result)

        self.after(100, wait_complete)

    def _show_update_result(self, result: dict):
        """업데이트 확인 결과 표시"""
        if result.get("error"):
            messagebox.showerror("업데이트 오류",
                               f"업데이트 확인 중 오류가 발생했습니다:\n\n{result['error']}")
            return

        if not result.get("has_update"):
            messagebox.showinfo("업데이트",
                              f"최신 버전을 사용 중입니다.\n\n현재 버전: {version.VERSION}")
            return

        # 새 버전 발견
        release_info = result["release_info"]
        new_version = release_info["version"]
        release_notes = release_info.get("body", "릴리즈 노트 없음")

        # 업데이트 확인 다이얼로그
        update_win = tk.Toplevel(self)
        update_win.title("새 버전 발견")
        update_win.configure(bg=_C["bg"])
        update_win.resizable(False, False)
        update_win.transient(self)
        update_win.grab_set()

        # 헤더
        hdr_frame = tk.Frame(update_win, bg=_C["panel"], pady=15)
        hdr_frame.pack(fill="x")

        tk.Label(hdr_frame, text="🎉 새 버전이 출시되었습니다!",
                font=self._font_title, bg=_C["panel"], fg=_C["blue"]).pack()
        tk.Label(hdr_frame, text=f"{version.VERSION}  →  {new_version}",
                font=self._font_body, bg=_C["panel"], fg=_C["green"]).pack(pady=(5, 0))

        # 릴리즈 노트
        notes_frame = tk.Frame(update_win, bg=_C["bg"])
        notes_frame.pack(fill="both", expand=True, padx=20, pady=15)

        tk.Label(notes_frame, text="릴리즈 노트:",
                font=self._font_small, bg=_C["bg"], fg=_C["text_dim"]).pack(anchor="w")

        notes_text = scrolledtext.ScrolledText(notes_frame, font=self._font_mono,
                                               bg=_C["log_bg"], fg=_C["text"],
                                               height=10, width=60, wrap="word",
                                               relief="flat", bd=0)
        notes_text.pack(fill="both", expand=True, pady=(5, 0))
        notes_text.insert("1.0", release_notes)
        notes_text.configure(state="disabled")

        # 버튼
        btn_frame = tk.Frame(update_win, bg=_C["bg"])
        btn_frame.pack(pady=(10, 20))

        def download_update():
            update_win.destroy()
            self._download_and_install(release_info)

        def cancel():
            update_win.destroy()

        tk.Button(btn_frame, text="업데이트 다운로드 및 설치",
                 command=download_update, font=self._font_body,
                 bg=_C["btn_green"], fg="white", padx=15, pady=8,
                 relief="flat", cursor="hand2").pack(side="left", padx=5)

        tk.Button(btn_frame, text="나중에",
                 command=cancel, font=self._font_body,
                 bg=_C["panel"], fg=_C["text"], padx=15, pady=8,
                 relief="flat", cursor="hand2").pack(side="left", padx=5)

        # 중앙 배치
        update_win.update_idletasks()
        w = update_win.winfo_reqwidth()
        h = update_win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        update_win.geometry(f"+{x}+{y}")

    def _download_and_install(self, release_info: dict):
        """업데이트 다운로드 및 설치"""
        # 다운로드 URL 확인
        checker = updater.UpdateChecker()
        download_url = checker.get_download_url(release_info)

        if not download_url:
            messagebox.showerror("다운로드 오류", "다운로드 링크를 찾을 수 없습니다.")
            return

        # 다운로드 진행 다이얼로그
        download_win = tk.Toplevel(self)
        download_win.title("업데이트 다운로드")
        download_win.configure(bg=_C["bg"])
        download_win.resizable(False, False)
        download_win.transient(self)
        download_win.grab_set()

        msg_frame = tk.Frame(download_win, bg=_C["bg"])
        msg_frame.pack(fill="both", expand=True, padx=30, pady=20)

        status_var = tk.StringVar(value="⏳ 다운로드 중...")
        tk.Label(msg_frame, textvariable=status_var,
                font=self._font_body, bg=_C["bg"], fg=_C["text"]).pack(pady=10)

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(msg_frame, variable=progress_var,
                                       maximum=100, length=400)
        progress_bar.pack(pady=5)

        percent_var = tk.StringVar(value="0%")
        tk.Label(msg_frame, textvariable=percent_var,
                font=self._font_small, bg=_C["bg"], fg=_C["text_dim"]).pack()

        # 중앙 배치
        download_win.update_idletasks()
        w = download_win.winfo_reqwidth()
        h = download_win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        download_win.geometry(f"+{x}+{y}")

        result = {"path": None, "error": None}

        def progress_callback(received, total):
            """다운로드 진행률 콜백"""
            if total > 0:
                percent = (received / total) * 100
                self.after(0, lambda: progress_var.set(percent))
                self.after(0, lambda: percent_var.set(f"{percent:.1f}%"))

        def download_in_thread():
            """백그라운드에서 다운로드"""
            try:
                downloader = updater.UpdateDownloader()
                installer_path = downloader.download(download_url, progress_callback)
                result["path"] = installer_path
            except Exception as e:
                result["error"] = str(e)

        # 스레드 시작
        thread = threading.Thread(target=download_in_thread, daemon=True)
        thread.start()

        def wait_download():
            if thread.is_alive():
                self.after(100, wait_download)
            else:
                download_win.destroy()
                if result.get("error"):
                    messagebox.showerror("다운로드 오류",
                                       f"다운로드 중 오류가 발생했습니다:\n\n{result['error']}")
                elif result.get("path"):
                    self._install_update(result["path"])

        self.after(100, wait_download)

    def _install_update(self, installer_path: str):
        """업데이트 설치"""
        msg = (
            "업데이트 다운로드가 완료되었습니다.\n\n"
            "지금 설치하시겠습니까?\n\n"
            "⚠️ 설치 시 RaidBook이 종료됩니다."
        )

        if messagebox.askyesno("업데이트 설치", msg):
            log.info(f"업데이트 설치 시작: {installer_path}")

            # 백업 생성
            status_msg = "백업 생성 중..."
            log.info(status_msg)

            if updater.create_backup():
                log.info("백업 완료")

            # 인스톨러 실행
            if updater.UpdateInstaller.install(installer_path, silent=False):
                log.info("인스톨러 실행됨. 프로그램 종료.")
                self._on_close()
            else:
                messagebox.showerror("설치 오류", "인스톨러 실행에 실패했습니다.")

    # ── 창 닫기 (X버튼) ───────────────────────────────────────────────────
    def _on_window_close(self):
        """X버튼 클릭 시 최소화/종료 선택 팝업 표시"""
        # 커스텀 선택 다이얼로그
        dialog = tk.Toplevel(self)
        dialog.title("RaidBook Watchdog")
        dialog.configure(bg=_C["bg"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        result = {"action": None}

        def minimize():
            result["action"] = "minimize"
            dialog.destroy()

        def close():
            result["action"] = "close"
            dialog.destroy()

        # 메시지
        msg_frame = tk.Frame(dialog, bg=_C["bg"])
        msg_frame.pack(fill="x", padx=20, pady=(20, 10))

        tk.Label(msg_frame, text="RaidBook Watchdog",
                font=self._font_title, bg=_C["bg"], fg=_C["blue"]).pack()

        # 버튼
        btn_frame = tk.Frame(dialog, bg=_C["bg"])
        btn_frame.pack(padx=20, pady=(10, 20))

        # 최소화 버튼 (트레이 사용 가능할 때만)
        if _TRAY_AVAILABLE:
            btn_minimize = tk.Button(btn_frame, text="최소화",
                                    command=minimize, font=self._font_body,
                                    bg=_C["btn_blue"], fg="white", padx=20, pady=8,
                                    relief="flat", cursor="hand2")
            btn_minimize.grid(row=0, column=0, padx=6, pady=3)

        # 종료 버튼
        btn_close = tk.Button(btn_frame, text="종료",
                             command=close, font=self._font_body,
                             bg=_C["btn_red"], fg="white", padx=20, pady=8,
                             relief="flat", cursor="hand2")
        btn_close.grid(row=0, column=1 if _TRAY_AVAILABLE else 0, padx=6, pady=3)

        # 중앙 배치
        dialog.update_idletasks()
        w = dialog.winfo_reqwidth()
        h = dialog.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        dialog.geometry(f"+{x}+{y}")

        # 다이얼로그 대기
        dialog.wait_window()

        # 결과 처리
        if result["action"] == "minimize":
            self._minimize_to_tray()
        elif result["action"] == "close":
            self._on_close()
        # cancel이면 아무것도 안 함

    # ── 종료 ──────────────────────────────────────────────────────────────
    def _on_close(self):
        # 재진입 방지 (종료 버튼/트레이/X버튼 중복 클릭 시 두 번 실행되는 것 차단)
        if getattr(self, "_closing", False):
            return
        self._closing = True
        log.info("Watchdog 종료 중 (GUI 닫기)...")

        # 어떤 단계에서 예외가 나도 반드시 os._exit까지 도달하도록 전부 방어.
        try:
            network_monitor_stop.set()
        except Exception:
            pass
        # 봇들에 즉시 종료 신호만 보내고 기다리지 않는다.
        # (블로킹 wait 제거 — 남은 프로세스는 os._exit + Job Object가 정리)
        try:
            with bot_states_lock:
                for name, st in list(bot_states.items()):
                    st["stopped"] = True
                    p = st.get("process")
                    if p and p.poll() is None:
                        try:
                            p.terminate()
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)


# ══════════════════════════════════════════════════════════════════════════
#  진입점
# ══════════════════════════════════════════════════════════════════════════
def _ensure_single_instance() -> None:
    """이미 실행 중인 인스턴스가 있으면 해당 창을 앞으로 가져오고 즉시 종료한다.

    Windows Named Mutex 를 사용해 중복 실행을 감지한다.
    기존 창은 WM_APP+1 메시지로 활성화 요청을 보낸다.
    """
    import ctypes
    import ctypes.wintypes

    MUTEX_NAME   = "RaidBook_Watchdog_GUI_SingleInstance"
    WM_APP_RAISE = 0x8001   # WM_APP + 1  (커스텀 메시지)
    ERROR_ALREADY_EXISTS = 183

    # use_last_error=True 필수 — windll 공유 인스턴스의 GetLastError()는
    # 파이썬 런타임의 다른 Win32 호출에 오염되어 중복 감지를 놓칠 수 있다.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32   = ctypes.windll.user32

    ctypes.set_last_error(0)
    _mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # ── 이미 실행 중 → 기존 창 활성화 후 종료 ──
        # 창 제목으로 핸들 탐색 (WatchdogApp 의 title 과 일치해야 함)
        hwnd = user32.FindWindowW(None, "RaidBook Watchdog")
        if hwnd:
            # 최소화 상태라면 복원
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        else:
            # 창을 찾지 못한 경우 — tkinter 알림으로 대체
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _mb.showwarning("RaidBook Watchdog",
                            "RaidBook Watchdog GUI가 이미 실행 중입니다.")
            _r.destroy()
        kernel32.CloseHandle(_mutex)
        sys.exit(0)

    # 이 프로세스가 최초 인스턴스 — 뮤텍스를 프로세스 수명 동안 유지
    # (전역 보관으로 GC 방지)
    import builtins
    builtins._watchdog_mutex = _mutex   # noqa: SLF001


def main():
    # Windows 앱 ID 등록 — 트레이·작업표시줄에 "python" 대신 앱 이름 표시
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RaidBook.Watchdog.GUI.1.0"
        )
    except Exception:
        pass

    _ensure_single_instance()

    log.info("=" * 50)
    log.info("RaidBook Watchdog GUI 시작")
    log.info(f"Watchdog PID: {os.getpid()}")
    log.info(f"Python 실행 파일: {sys.executable}")
    log.info(f"감시 대상: {len(BOTS)}개 봇")
    log.info(f"재시작 딜레이: {RESTART_DELAY}초")
    log.info(f"빠른 재시작 제한: {RAPID_RESTART_WINDOW}초 내 {MAX_RAPID_RESTARTS}회")
    if NETWORK_CHECK_ENABLED:
        targets = ", ".join(
            f"{t['host']}:{t['port']}" for t in normalize_network_targets(NETWORK_CHECK_TARGETS)
        )
        log.info(f"네트워크 체크: {NETWORK_CHECK_INTERVAL}초마다 {targets}")
    else:
        log.info("네트워크 체크: 비활성화")
    log.info("=" * 50)

    # 네트워크 모니터 시작
    start_network_monitor()

    # Discord Watchdog 봇 + 서브봇 시작
    # 마법사 모드일 때는 _finish_init() 완료 후 시작하므로 여기서는 생략
    if not _WIZARD_MODE:
        start_discord_bot_thread()
        start_all_enabled_bots()
    else:
        log.info("[마법사] 초기 설정이 필요합니다 — 설정 완료 후 봇을 시작합니다.")

    # GUI 시작 (메인 스레드)
    app = WatchdogApp()
    app.mainloop()


if __name__ == "__main__":
    main()
