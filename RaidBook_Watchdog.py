# -*- coding: utf-8 -*-
"""
RaidBook Watchdog - 봇 프로세스 감시 및 자동 재시작 (공유 코어)

이 모듈은 Watchdog의 모든 공통 로직(Discord 명령 봇, 프로세스 감시,
네트워크 체크, IPC 포워딩, 정산/설문/참석통계 등)을 담는 단일 소스다.

- 콘솔 모드: `python RaidBook_Watchdog.py` 로 직접 실행 (CMD 입력 루프).
- GUI 모드: RaidBook_Watchdog_GUI.py 가 이 모듈을 import 해서
  Discord 봇/프로세스 관리 로직을 그대로 재사용하고, tkinter 트레이/토스트/
  설정 마법사 같은 GUI 전용 기능만 위에 얹는다.

GUI는 `set_notify_hook()` / `set_bot_output_hook()` 으로 알림 토스트와
봇별 상세 로그 뷰어에 필요한 콜백만 등록한다. 이 모듈 자체는 tkinter에
대해 전혀 알지 못한다 (순환 의존성 방지).

명령어 (콘솔 입력 루프): restart 봇이름 / stop 봇이름 / start 봇이름 / status / help
config: Config/watchdog_config.json
"""
import subprocess
import time
import re
import os
import sys
import json
import logging
import socket
import threading
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands as dc_commands
from RaidBook_common import DailyFileHandler
from config_helper import (
    ensure_config_from_template,
    ensure_runtime_tree,
    get_app_dir,
    get_config_path,
    get_runtime_dir,
    is_frozen,
)

_SCRIPT_DIR = get_app_dir()
_RUNTIME_DIR = get_runtime_dir()
ensure_runtime_tree()
CONFIG_PATH = get_config_path("watchdog_config.json")

# 설정 파일 자동 생성 (템플릿에서)
if not ensure_config_from_template(CONFIG_PATH):
    print(f"❌ 설정 파일을 찾을 수 없습니다: {CONFIG_PATH}", file=sys.stderr)
    print(f"   템플릿 파일도 없습니다. 프로그램을 종료합니다.", file=sys.stderr)
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

BASE_DIR = config.get("base_dir") or _RUNTIME_DIR
LOG_DIR = os.path.join(BASE_DIR, "Log")
os.makedirs(LOG_DIR, exist_ok=True)

# ── 로그 ──────────────────────────────────────────────────────────────────
log = logging.getLogger("Watchdog")
log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
log.addHandler(_console)

_file = DailyFileHandler(LOG_DIR, "RaidBook_Watchdog")
_file.setFormatter(_formatter)
log.addHandler(_file)

# ── 설정 ──────────────────────────────────────────────────────────────────
BOTS = config.get("bots", [])
# 음악 봇(v1.0.17~)은 기존 설치본 config에 없으므로 자동으로 감시 대상에 추가 (끄려면 enabled=false)
if not any(str(b.get("script", "")).lower() == "raidbook_music_bot.py" for b in BOTS):
    BOTS.append({"name": "Music Bot", "script": "RaidBook_Music_Bot.py", "enabled": True})
RESTART_DELAY = config.get("restart_delay_seconds", 5)
MAX_RAPID_RESTARTS = config.get("max_rapid_restarts", 5)
RAPID_RESTART_WINDOW = config.get("rapid_restart_window_seconds", 60)

# 봇 이름 → IPC 포트 매핑 (포트 해제 대기에 사용)
_IPC_PORTS: dict = {}
_ipc_cfg = config.get("ipc", {})
_BOT_KEY_MAP = {"Boss Bot": "boss"}
for _bn, _bk in _BOT_KEY_MAP.items():
    if _bk in _ipc_cfg:
        _IPC_PORTS[_bn] = int(_ipc_cfg[_bk])

NETWORK_CHECK_CONFIG = config.get("network_check", {})
NETWORK_CHECK_ENABLED = NETWORK_CHECK_CONFIG.get("enabled", True)
NETWORK_CHECK_INTERVAL = NETWORK_CHECK_CONFIG.get("interval_seconds", 60)
NETWORK_CHECK_TIMEOUT = NETWORK_CHECK_CONFIG.get("timeout_seconds", 5)
NETWORK_CHECK_CONNECT_ATTEMPTS = NETWORK_CHECK_CONFIG.get("connect_attempts", 2)
DEFAULT_NETWORK_TARGETS = [
    {"name": "Discord Gateway", "host": "gateway.discord.gg", "port": 443},
    {"name": "Discord API", "host": "discord.com", "port": 443},
]
NETWORK_CHECK_TARGETS = NETWORK_CHECK_CONFIG.get("targets", DEFAULT_NETWORK_TARGETS)

# 봇 상태 관리: {name: {"process": Process, "thread": Thread, "enabled": bool, "stopped": bool, ...}}
bot_states = {}
bot_states_lock = threading.Lock()
network_status_lock = threading.Lock()
network_monitor_stop = threading.Event()
last_network_results = []
last_network_checked_at = None

EXIT_CODE_HINTS = {
    0: "정상 종료",
    1: "일반 오류 또는 Watchdog terminate/kill에 의한 종료 가능",
    0xC000013A: "Windows 콘솔 종료/Ctrl+C/세션 종료 계열",
}


# ── GUI 연동 훅 (기본값은 no-op — 콘솔 모드에서는 아무 일도 하지 않는다) ─────
def _default_notify_hook(title, msg):
    pass


def _default_bot_output_hook(name, line):
    pass


_notify_hook = _default_notify_hook
_bot_output_hook = _default_bot_output_hook


# 원격 패널 📜 로그 버튼용 — 봇별 최근 출력 줄 (메모리, 재시작 시 초기화)
_RECENT_OUTPUT_MAX = 300
_recent_output = {}
_recent_output_lock = threading.Lock()


def _remember_output(name, line):
    with _recent_output_lock:
        buf = _recent_output.get(name)
        if buf is None:
            buf = _recent_output[name] = deque(maxlen=_RECENT_OUTPUT_MAX)
        buf.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")


def recent_output(name, count=30):
    with _recent_output_lock:
        return list(_recent_output.get(name, ()))[-count:]


def set_notify_hook(fn):
    """GUI가 트레이/토스트 알림을 처리하려면 fn(title, msg)를 등록한다."""
    global _notify_hook
    _notify_hook = fn or _default_notify_hook


def set_bot_output_hook(fn):
    """GUI가 봇별 상세 로그 뷰어를 운영하려면 fn(bot_name, line)을 등록한다."""
    global _bot_output_hook
    _bot_output_hook = fn or _default_bot_output_hook


def _notify(title, msg):
    try:
        _notify_hook(title, msg)
    except Exception:
        pass


def format_exit_status(exit_code):
    if exit_code is None:
        return "코드: 알 수 없음"
    win_code = exit_code + (1 << 32) if exit_code < 0 else exit_code
    hex_code = f"0x{win_code:08X}"
    hint = EXIT_CODE_HINTS.get(exit_code) or EXIT_CODE_HINTS.get(win_code)
    if hint:
        return f"코드: {exit_code}, {hex_code}, 원인 추정: {hint}"
    return f"코드: {exit_code}, {hex_code}"


# ── 네트워크 체크 ──────────────────────────────────────────────────────────
def normalize_network_targets(targets):
    normalized = []
    for target in targets:
        if isinstance(target, str):
            normalized.append({"name": target, "host": target, "port": 443})
            continue
        host = target.get("host")
        if not host:
            continue
        normalized.append({
            "name": target.get("name", host),
            "host": host,
            "port": int(target.get("port", 443)),
        })
    return normalized


def check_tcp_target(target):
    started = time.perf_counter()
    host = target["host"]
    port = target["port"]
    attempts = 0
    try:
        addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        last_error = None
        seen_addresses = set()
        for family, socktype, proto, _, sockaddr in addr_infos:
            address_key = (family, sockaddr)
            if address_key in seen_addresses:
                continue
            seen_addresses.add(address_key)

            attempts += 1
            try:
                with socket.socket(family, socktype, proto) as sock:
                    sock.settimeout(NETWORK_CHECK_TIMEOUT)
                    sock.connect(sockaddr)
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                return {**target, "ok": True, "elapsed_ms": elapsed_ms, "attempts": attempts, "error": None}
            except OSError as e:
                last_error = e
                if attempts >= NETWORK_CHECK_CONNECT_ATTEMPTS:
                    break

        if last_error:
            raise last_error
        raise OSError("no usable address")
    except OSError as e:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            **target,
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "attempts": attempts,
            "error": f"{type(e).__name__}: {e}",
        }


def run_network_checks():
    global last_network_results, last_network_checked_at

    results = [check_tcp_target(target) for target in normalize_network_targets(NETWORK_CHECK_TARGETS)]
    checked_at = datetime.now()

    with network_status_lock:
        last_network_results = results
        last_network_checked_at = checked_at

    return results


def network_signature(results):
    return tuple((r["host"], r["port"], r["ok"]) for r in results)


def log_network_results(title, results):
    log.info(f"[네트워크] {title}")
    for result in results:
        target = f"{result['name']} ({result['host']}:{result['port']})"
        if result["ok"]:
            log.info(f"[네트워크] {target} 연결 OK ({result['elapsed_ms']}ms, 시도 {result['attempts']}회)")
        else:
            log.info(f"[네트워크] {target} 연결 실패 ({result['elapsed_ms']}ms, 시도 {result['attempts']}회): {result['error']}")

    if any(not r["ok"] for r in results):
        log.info("[네트워크] Discord TCP 연결 실패 시 reconnect TimeoutError는 봇 코드보다 방화벽/VPN/프록시/망 경로 가능성이 큼")


def network_monitor_loop():
    if not NETWORK_CHECK_ENABLED:
        log.info("[네트워크] 주기 체크 비활성화")
        return

    previous_signature = None
    while not network_monitor_stop.is_set():
        results = run_network_checks()
        signature = network_signature(results)
        if previous_signature is None:
            log_network_results("초기 체크", results)
        elif signature != previous_signature:
            log_network_results("상태 변경", results)
        previous_signature = signature

        network_monitor_stop.wait(NETWORK_CHECK_INTERVAL)


def start_network_monitor():
    t = threading.Thread(target=network_monitor_loop, daemon=True)
    t.start()
    return t


def print_network_status(force_check=False):
    if force_check:
        results = run_network_checks()
        log_network_results("수동 체크", results)
        return

    with network_status_lock:
        results = list(last_network_results)
        checked_at = last_network_checked_at

    if not results:
        log.info("[네트워크] 아직 체크 결과 없음")
        return

    checked_text = checked_at.strftime("%Y-%m-%d %H:%M:%S") if checked_at else "알 수 없음"
    log.info(f"[네트워크] 최근 체크: {checked_text}")
    for result in results:
        status = "OK" if result["ok"] else "실패"
        log.info(f"  {result['name']}: {status} ({result['host']}:{result['port']}, {result['elapsed_ms']}ms, 시도 {result['attempts']}회)")


# ── 봇 프로세스 관리 ───────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
#  Windows Job Object — 봇 자식 프로세스를 Watchdog 수명에 묶는다.
#  KILL_ON_JOB_CLOSE 플래그로, Watchdog(부모)이 크래시·강제종료 등 어떤
#  이유로든 죽으면 OS가 job에 속한 모든 봇 프로세스를 자동 종료시킨다.
#  → 부모가 죽고 봇만 살아남는 "고아 프로세스" + 다음 실행 시 중복 spawn 방지.
# ──────────────────────────────────────────────────────────────────────────
_job_handle = None


def _init_job_object():
    """봇들을 묶을 Job Object를 생성하고 KILL_ON_JOB_CLOSE를 설정한다 (Windows 전용)."""
    global _job_handle
    if _job_handle is not None:
        return
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            log.info("[Job] CreateJobObject 실패 — 봇 자동정리 비활성")
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            log.info("[Job] SetInformationJobObject 실패 — 봇 자동정리 비활성")
            kernel32.CloseHandle(job)
            return

        _job_handle = job
        log.info("[Job] Job Object 생성 — 봇은 Watchdog 종료 시 함께 종료됨")
    except Exception as e:
        log.info(f"[Job] 초기화 오류: {e} — 봇 자동정리 비활성")
        _job_handle = None


def _assign_to_job(process):
    """spawn한 봇 프로세스를 Job Object에 등록한다."""
    if _job_handle is None or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        # subprocess가 CreateProcess로 받은 프로세스 핸들을 그대로 사용
        if not kernel32.AssignProcessToJobObject(_job_handle, int(process._handle)):
            log.info(f"[Job] 프로세스 {process.pid} job 등록 실패 (err={ctypes.get_last_error()})")
    except Exception as e:
        log.info(f"[Job] job 등록 오류: {e}")


def _wait_port_free(port, timeout=20.0, interval=0.5):
    """포트가 사용 중이면 최대 timeout초 대기 후 해제 여부를 반환한다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            result = s.connect_ex(("127.0.0.1", port))
        if result != 0:   # 연결 실패 = 포트 비어있음
            return True
        time.sleep(interval)
    return False   # 타임아웃


def run_bot(name):
    """봇 프로세스를 실행하고 감시"""
    bot_config = next((b for b in BOTS if b["name"] == name), None)
    if not bot_config:
        return

    # PyInstaller 빌드 환경에서는 .exe 사용, 개발 환경에서는 .py 사용
    script = bot_config["script"]

    # .exe 파일 경로 (빌드 환경)
    exe_name = script.replace(".py", ".exe")
    exe_path = os.path.join(_SCRIPT_DIR, exe_name)

    # .py 파일 경로 (개발 환경)
    py_path = os.path.join(_SCRIPT_DIR, script)

    # .exe가 있으면 사용, 개발 환경에서만 .py fallback 사용.
    # frozen 상태에서 sys.executable은 RaidBook.exe이므로 .py fallback은 자기 자신 재실행 루프를 만든다.
    if os.path.exists(exe_path):
        executable = exe_path
        use_python = False
    elif not is_frozen() and os.path.exists(py_path):
        executable = py_path
        use_python = True
    else:
        log.info(f"[{name}] 실행 파일 없음: {exe_path} 또는 {py_path}")
        return

    restart_times = []

    while True:
        with bot_states_lock:
            state = bot_states.get(name, {})
            if state.get("stopped"):
                log.info(f"[{name}] 중지 상태 — 대기")
                return

        now = time.time()
        restart_times = [t for t in restart_times if now - t < RAPID_RESTART_WINDOW]
        if len(restart_times) >= MAX_RAPID_RESTARTS:
            log.info(f"[{name}] {RAPID_RESTART_WINDOW}초 내 {MAX_RAPID_RESTARTS}회 재시작 — 감시 중단!")
            with bot_states_lock:
                bot_states[name]["stopped"] = True
                bot_states[name]["rapid_limit"] = True
            _notify(f"⚠ {name} 감시 중단",
                    f"{RAPID_RESTART_WINDOW}초 내 {MAX_RAPID_RESTARTS}회 재시작 — 수동 시작이 필요합니다.")
            break

        log.info(f"[{name}] 시작: {executable}")

        # IPC 포트가 아직 점유 중이면 해제될 때까지 대기 (재시작/Watchdog 재가동 시 충돌 방지)
        _ipc_port = _IPC_PORTS.get(name)
        if _ipc_port:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.settimeout(0.3)
                _in_use = _s.connect_ex(("127.0.0.1", _ipc_port)) == 0
            if _in_use:
                log.info(f"[{name}] IPC 포트 {_ipc_port} 사용 중 — 해제 대기 (최대 20초)")
                if not _wait_port_free(_ipc_port, timeout=20.0):
                    log.info(f"[{name}] 포트 {_ipc_port} 미해제 — 강제 진행")
                else:
                    log.info(f"[{name}] 포트 {_ipc_port} 해제 확인 — 시작")

        try:
            # 서브프로세스 환경: UTF-8 출력 강제 (Windows 한국어 환경에서 CP949 깨짐 방지)
            _bot_env = os.environ.copy()
            _bot_env["PYTHONIOENCODING"] = "utf-8"   # stdout/stderr 인코딩 고정
            _bot_env["PYTHONUTF8"] = "1"             # Python 3.7+ UTF-8 모드
            _bot_env["RAIDBOOK_MANAGED"] = "1"       # Watchdog 관리 표시 — 봇 !restart가 execv 대신 종료(=Watchdog 재시작) 선택

            # .exe 또는 .py 실행
            if use_python:
                cmd = [sys.executable, "-u", executable]
            else:
                cmd = [executable]

            process = subprocess.Popen(
                cmd,
                cwd=_SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                env=_bot_env,
            )
            # Watchdog가 죽으면 이 봇도 함께 종료되도록 Job Object에 등록
            _assign_to_job(process)
            with bot_states_lock:
                bot_states[name]["process"] = process
                bot_states[name]["start_time"] = datetime.now()
                cnt = bot_states[name].get("restart_count", 0)
                if cnt > 0:   # 첫 시작은 카운트 안 함
                    _notify(f"🔄 {name} 재시작", f"{name}이(가) 재시작됐습니다. (총 {cnt}회)")
                bot_states[name]["restart_count"] = cnt + 1 if cnt > 0 else 0

            # stdout 읽기 스레드
            def _read_stdout(proc, bot_name):
                try:
                    for raw in proc.stdout:
                        line = raw.rstrip("\n\r")
                        if line:
                            _remember_output(bot_name, line)
                            _bot_output_hook(bot_name, line)
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass

            rt = threading.Thread(target=_read_stdout, args=(process, name), daemon=True)
            rt.start()

            process.wait()
            rt.join(timeout=3)
            exit_code = process.returncode
            log.info(f"[{name}] 종료 ({format_exit_status(exit_code)})")
            # 비정상 종료 알림 (0=정상, 수동 중지 제외)
            with bot_states_lock:
                _stopped = bot_states.get(name, {}).get("stopped", False)
            if not _stopped and exit_code not in (0, None):
                _notify(f"❌ {name} 비정상 종료", f"종료 코드: {exit_code}  —  자동 재시작 예정")
        except Exception as e:
            log.info(f"[{name}] 실행 오류: {e}")

        with bot_states_lock:
            bot_states[name]["process"] = None
            bot_states[name]["start_time"] = None
            if bot_states[name].get("stopped"):
                return

        # enabled 토글 체크 — 비활성화 시 재시작 안 함
        with bot_states_lock:
            if not bot_states.get(name, {}).get("enabled", True):
                log.info(f"[{name}] 자동 재시작 비활성화 — 중지 유지")
                bot_states[name]["stopped"] = True
                return

        restart_times.append(time.time())
        with bot_states_lock:
            bot_states[name]["restart_count"] = bot_states[name].get("restart_count", 0) + 1
        log.info(f"[{name}] {RESTART_DELAY}초 후 재시작...")
        time.sleep(RESTART_DELAY)


def start_bot(name):
    """봇 감시 스레드 시작"""
    with bot_states_lock:
        state = bot_states.get(name)
        if state and not state.get("stopped"):
            log.info(f"[{name}] 이미 실행 중")
            return False

        bot_states[name] = {
            "process": None,
            "thread": None,
            "stopped": False,
            "start_time": None,
            "restart_count": 0,
            "rapid_limit": False,
            "enabled": True,
        }

    t = threading.Thread(target=run_bot, args=(name,), daemon=True)
    t.start()
    with bot_states_lock:
        bot_states[name]["thread"] = t
    log.info(f"[{name}] 감시 시작")
    return True


def stop_bot(name):
    """봇 프로세스 중지"""
    with bot_states_lock:
        state = bot_states.get(name)
        if not state:
            log.info(f"[{name}] 실행 중이 아님")
            return False
        state["stopped"] = True
        process = state.get("process")

    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log.info(f"[{name}] 프로세스 종료")
    else:
        log.info(f"[{name}] 중지 플래그 설정")
    return True


def restart_bot(name):
    """봇 재시작 (중지 → 시작)"""
    stop_bot(name)
    time.sleep(2)
    return start_bot(name)


def find_bot_name(input_name):
    """입력된 이름으로 봇 이름 매칭 (부분 매칭)"""
    input_lower = input_name.lower()
    for b in BOTS:
        if b["name"].lower() == input_lower:
            return b["name"]
    for b in BOTS:
        if input_lower in b["name"].lower():
            return b["name"]
    return None


def print_status():
    """모든 봇 상태 출력"""
    with bot_states_lock:
        log.info("=" * 40)
        log.info("봇 상태:")
        for b in BOTS:
            name = b["name"]
            state = bot_states.get(name, {})
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
    print_network_status()


def print_help():
    log.info("=" * 40)
    log.info("Watchdog 명령어:")
    log.info("  status              - 봇 상태 확인")
    log.info("  restart 봇이름      - 특정 봇 재시작")
    log.info("  restart all         - 모든 봇 재시작")
    log.info("  stop 봇이름         - 특정 봇 중지")
    log.info("  stop all            - 모든 봇 중지")
    log.info("  start 봇이름        - 중지된 봇 시작")
    log.info("  start all           - 모든 봇 시작")
    log.info("  list                - 봇 목록")
    log.info("  netcheck            - Discord TCP 연결 확인")
    log.info("  help                - 도움말")
    log.info("  quit                - Watchdog 종료")
    log.info("=" * 40)


def command_loop():
    """CMD 명령어 입력 루프 (콘솔 모드 전용)"""
    time.sleep(2)  # 봇 시작 대기
    print_help()

    while True:
        try:
            cmd = input("\n[Watchdog] > ").strip()
        except (EOFError, KeyboardInterrupt):
            log.info("Watchdog 종료 요청 감지 (콘솔 입력 종료/Ctrl+C)")
            network_monitor_stop.set()
            for b in BOTS:
                stop_bot(b["name"])
            os._exit(0)

        if not cmd:
            continue

        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""

        if action == "status":
            print_status()

        elif action == "restart":
            if target.lower() == "all":
                for b in BOTS:
                    restart_bot(b["name"])
            elif target:
                name = find_bot_name(target)
                if name:
                    restart_bot(name)
                else:
                    log.info(f"❌ 봇을 찾을 수 없음: {target}")
            else:
                log.info("사용법: restart 봇이름 또는 restart all")

        elif action == "stop":
            if target.lower() == "all":
                for b in BOTS:
                    stop_bot(b["name"])
            elif target:
                name = find_bot_name(target)
                if name:
                    stop_bot(name)
                else:
                    log.info(f"❌ 봇을 찾을 수 없음: {target}")
            else:
                log.info("사용법: stop 봇이름 또는 stop all")

        elif action == "start":
            if target.lower() == "all":
                for b in BOTS:
                    if b.get("enabled", True):
                        start_bot(b["name"])
            elif target:
                name = find_bot_name(target)
                if name:
                    start_bot(name)
                else:
                    log.info(f"❌ 봇을 찾을 수 없음: {target}")
            else:
                log.info("사용법: start 봇이름 또는 start all")

        elif action == "list":
            log.info("봇 목록:")
            for b in BOTS:
                enabled = "활성" if b.get("enabled", True) else "비활성"
                log.info(f"  • {b['name']} ({b['script']}) — {enabled}")

        elif action in ("netcheck", "network"):
            print_network_status(force_check=True)

        elif action == "help":
            print_help()

        elif action in ("quit", "exit"):
            log.info("Watchdog 종료 중...")
            network_monitor_stop.set()
            for b in BOTS:
                stop_bot(b["name"])
            time.sleep(2)
            os._exit(0)

        else:
            log.info(f"❌ 알 수 없는 명령어: {cmd} (help로 도움말 확인)")


# === Watchdog Discord Bot ===
WATCHDOG_TOKEN = config.get("token", "")
ADMIN_CHANNEL_ID = int(config.get("admin_channel_id") or 0)
_UPDATE_CHECK_CFG = config.get("update_check", {})
UPDATE_CHECK_ENABLED = bool(_UPDATE_CHECK_CFG.get("enabled", True))
UPDATE_CHECK_INTERVAL_HOURS = float(_UPDATE_CHECK_CFG.get("interval_hours", 6) or 6)
_IPC_MAP = config.get("ipc", {})

_BOT_ALIASES = {
    "boss": "Boss Bot",
}

# 봇별 명령어 목록  {prefix: [(cmd, 설명), ...]} — GUI 명령어 패널에서도 사용
_BOT_COMMANDS = {
    "boss": [
        ("listboss", "현재 보스 스케줄 목록"),
        ("startboss [이름] [HH:MM] [주기]", "보스 스케줄 시작"),
        ("stopboss [이름]", "보스 스케줄 중지 (생략 시 전체)"),
        ("setboss [이름] [시간]", "보스 주기 변경"),
        ("history [일수]", "히스토리 조회 (기본 7일)"),
        ("stats [일수|all] [상위N|all]", "참석자 통계 (기본 30일/25명)"),
        ("setrefresh [HH:MM]", "일일 정리 시각 변경"),
        ("create [이름] [HH:MM] [분]", "보스 메시지 생성"),
        ("close [메시지ID]", "보스 마감"),
        ("activate [메시지ID] [분]", "보스 재활성화"),
        ("extend [메시지ID] [분]", "마감 시간 연장"),
        ("reopen [날짜시간] [분]", "보스 재오픈"),
        ("rename [기존이름] [새이름]", "보스 이름 변경"),
        ("recover [메시지ID]", "메시지 복구"),
        ("sync [메시지ID]", "메시지 동기화"),
        ("delete [메시지ID]", "메시지 삭제"),
        ("listdailyboss", "매일 고정시간 반복 알림 목록"),
        ("startdailyboss [이름]", "config daily_bosses 보스 매일 반복 시작"),
        ("stopdailyboss [이름]", "매일 반복 알림 중지"),
        ("attachloot [메시지ID|all] [일수]", "기존 메시지에 획득 아이템 버튼 부착"),
        ("fixid", "UID 재매핑"),
        ("collectmembers", "서버 멤버 UID↔닉네임 수집"),
        ("test", "봇 동작 테스트"),
        ("save", "데이터 저장"),
        ("refresh", "데이터 새로고침"),
        ("restart [silent]", "Boss Bot 재시작"),
    ],
}

# 봇 이름 → 명령어 prefix 매핑}

# 봇 이름 → 명령어 prefix 매핑
_BOT_NAME_TO_PREFIX = {
    "Boss Bot": "boss",
}

_wdog_intents = discord.Intents.default()
_wdog_intents.message_content = True
_wdog_intents.members = True
_wdog_bot = dc_commands.Bot(command_prefix="!", intents=_wdog_intents)
_wdog_ready_announced = False
_wdog_slash_synced = False
_discord_loop = None   # Discord 봇 이벤트 루프 참조 (재시작 시 사용)

async def _send_ipc(bot_key, cmd, args=None):
    port = _IPC_MAP.get(bot_key)
    if not port:
        return {"ok": False, "error": f"알 수 없는 봇: {bot_key}"}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=5)
        payload = json.dumps({"cmd": cmd, "args": args or []}, ensure_ascii=False)
        writer.write(payload.encode("utf-8") + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=15)
        writer.close()
        return json.loads(data.decode("utf-8"))
    except asyncio.TimeoutError:
        return {"ok": False, "error": "응답 타임아웃 (봇이 바쁘거나 오프라인)"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "봇 IPC 연결 실패 (오프라인이거나 시작 중)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _reply_ipc(ctx, status_msg, result):
    if not result["ok"]:
        await status_msg.edit(content=f"❌ {result['error']}")
        return
    messages = result.get("messages", [])
    if not messages:
        await status_msg.edit(content="✅ 완료")
        return
    await status_msg.delete()
    for msg in messages:
        if not msg:
            continue
        for i in range(0, len(msg), 1900):
            await ctx.send(msg[i:i + 1900])


async def _sync_watchdog_slash_commands():
    global _wdog_slash_synced
    if _wdog_slash_synced:
        return
    try:
        synced = await _wdog_bot.tree.sync()
        log.info(f"[Watchdog Discord] 슬래시 명령 동기화 완료: {len(synced)}개")
    except Exception as e:
        log.info(f"[Watchdog Discord] 슬래시 명령 동기화 실패: {e}")

    guild_ok = 0
    guild_fail = 0
    for guild in _wdog_bot.guilds:
        try:
            _wdog_bot.tree.copy_global_to(guild=guild)
            guild_synced = await _wdog_bot.tree.sync(guild=guild)
            guild_ok += 1
            log.info(f"[Watchdog Discord] guild 슬래시 동기화 완료: {guild.name}({guild.id}) {len(guild_synced)}개")
        except Exception as e:
            guild_fail += 1
            log.info(f"[Watchdog Discord] guild 슬래시 동기화 실패: {guild.name}({guild.id}) | {e}")
    if _wdog_bot.guilds:
        log.info(f"[Watchdog Discord] guild 슬래시 동기화 요약: 성공 {guild_ok} / 실패 {guild_fail}")
    _wdog_slash_synced = True


_update_notified_version = None
_update_check_task = None


async def _update_check_loop():
    """주기적으로 GitHub 최신 릴리즈를 확인하고 새 버전이면 관리 채널에 알림 (버전당 1회)."""
    global _update_notified_version
    try:
        import RaidBook_updater as _updater
        import version as _version
    except ImportError:
        log.info("[업데이트] updater/version 모듈 없음 — 자동 확인 비활성")
        return
    await asyncio.sleep(30)  # 시작 직후 부하 회피
    while True:
        try:
            checker = _updater.UpdateChecker()
            has_update, release_info = await asyncio.get_event_loop().run_in_executor(
                None, checker.check_for_updates)
            if has_update and release_info:
                new_ver = release_info.get("version", "")
                if new_ver and new_ver != _update_notified_version:
                    _update_notified_version = new_ver
                    ch = _wdog_bot.get_channel(ADMIN_CHANNEL_ID)
                    if ch:
                        url = release_info.get("html_url", "")
                        body = (release_info.get("body") or "").strip()
                        lines = [
                            f"🎉 **RaidBook 새 버전 출시: v{new_ver}** (현재 v{_version.VERSION})",
                            f"릴리즈: {url}" if url else "",
                            "Watchdog GUI의 🔄 버튼으로 업데이트할 수 있습니다.",
                        ]
                        if body:
                            lines.append(f">>> {body[:500]}")
                        await ch.send("\n".join(l for l in lines if l))
                        log.info(f"[업데이트] 새 버전 v{new_ver} 알림 전송")
        except Exception as e:
            log.info(f"[업데이트] 자동 확인 실패 (무시): {e}")
        await asyncio.sleep(max(1, UPDATE_CHECK_INTERVAL_HOURS) * 3600)


async def _ensure_admin_channel():
    """관리 채널이 비었거나 삭제됐으면 'raidbook-관리'(비공개)를 만들어 config에 저장."""
    global ADMIN_CHANNEL_ID
    if not config.get("auto_create_channels", True):
        return
    try:
        from RaidBook_channel_setup import ChannelSpec, ensure_channels
        if not config.get("guild_id"):
            # 보스봇 config의 채널로 같은 서버를 찾는다
            try:
                with open(get_config_path("boss_config.json"), "r", encoding="utf-8") as f:
                    _boss_cfg = json.load(f)
                for key in ("alert_channel_id", "command_channel_id", "loot_channel_id"):
                    ch = _wdog_bot.get_channel(int(_boss_cfg.get(key) or 0))
                    if ch is not None:
                        config["guild_id"] = ch.guild.id
                        break
            except Exception:
                pass
        created, problems = await ensure_channels(
            _wdog_bot, CONFIG_PATH, config,
            [ChannelSpec("admin_channel_id", "raidbook-관리", private=True)], log)
        for p in problems:
            log.info(f"[Watchdog Discord] 관리 채널 자동 생성: {p}")
        ADMIN_CHANNEL_ID = int(config.get("admin_channel_id") or 0)
    except Exception as e:
        log.info(f"[Watchdog Discord] 관리 채널 자동 생성 오류: {e}")


@_wdog_bot.event
async def on_ready():
    global _wdog_ready_announced, _update_check_task
    log.info(f"[Watchdog Discord] {_wdog_bot.user} 연결됨")
    await _ensure_admin_channel()
    await _sync_watchdog_slash_commands()
    await _start_panel_services()
    if UPDATE_CHECK_ENABLED and ADMIN_CHANNEL_ID and (_update_check_task is None or _update_check_task.done()):
        _update_check_task = asyncio.create_task(_update_check_loop())
    if _wdog_ready_announced:
        log.info("[Watchdog Discord] 재연결 감지 — 시작 알림 생략")
        return
    try:
        ch = _wdog_bot.get_channel(ADMIN_CHANNEL_ID) or await _wdog_bot.fetch_channel(ADMIN_CHANNEL_ID)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"🟢 **Watchdog 시작됨** `{now}`", ""]
        for b in BOTS:
            enabled = b.get("enabled", True)
            lines.append(f"  • **{b['name']}** — {'감시 중' if enabled else '비활성'}")
        await ch.send("\n".join(lines))
        _wdog_ready_announced = True
    except Exception as e:
        log.info(f"[Watchdog Discord] 시작 알림 전송 실패: {e}")


@_wdog_bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id != ADMIN_CHANNEL_ID:
        if str(message.content or "").startswith("!"):
            log.info(
                f"[Watchdog Discord] 관리 채널 아님 — 명령 무시 "
                f"(channel={message.channel.id}, expected={ADMIN_CHANNEL_ID}, content={str(message.content)[:50]})"
            )
        return
    if not message.content:
        log.info("[Watchdog Discord] 메시지 수신했지만 내용이 비어 있음 — Discord Message Content Intent 확인 필요")
        return
    log.info(f"[Watchdog Discord] 메시지 수신: {message.content[:50]}")
    await _wdog_bot.process_commands(message)


@_wdog_bot.event
async def on_command_error(ctx, error):
    if isinstance(error, dc_commands.CommandNotFound):
        log.info(f"[Watchdog Discord] 알 수 없는 명령어: {ctx.message.content[:80]}")
        return
    log.error(f"[Watchdog Discord] 명령 처리 오류: {error}", exc_info=True)
    try:
        await ctx.send(f"❌ 명령 처리 중 오류가 발생했습니다: {error}")
    except Exception:
        pass


@_wdog_bot.command(name="watchdog", aliases=["wd"])
async def _wdog_cmd(ctx, action: str = "status", target: str = "all", *flags):
    action = action.lower()
    loop = asyncio.get_event_loop()
    silent = any(str(f).lower() in ("silent", "quiet", "s", "조용", "조용히") for f in flags)

    async def _say(content, edit_msg=None):
        """silent 모드면 send/edit 모두 no-op."""
        if silent:
            return None
        if edit_msg is not None:
            try:
                await edit_msg.edit(content=content)
                return edit_msg
            except Exception:
                return None
        try:
            return await ctx.send(content)
        except Exception:
            return None

    if action == "status":
        msg = await ctx.send("⏳ 상태 확인 중...")
        lines = ["**봇 상태**"]
        with bot_states_lock:
            for b in BOTS:
                name = b["name"]
                state = bot_states.get(name, {})
                process = state.get("process")
                stopped = state.get("stopped", True)
                if stopped:
                    status = "🔴 중지"
                elif process and process.poll() is None:
                    status = f"🟢 실행중 (PID: {process.pid})"
                else:
                    status = "🟡 재시작 대기"
                lines.append(f"  • **{name}**: {status}")
        await msg.edit(content="\n".join(lines))

    elif action == "restart":
        if target.lower() == "all":
            log.info(f"[wd restart all] silent={silent}")
            msg = await _say("⏳ 모든 봇 재시작 중...")
            await loop.run_in_executor(None, lambda: [restart_bot(b["name"]) for b in BOTS])
            await _say("✅ 모든 봇 재시작 완료", edit_msg=msg)
        else:
            name = find_bot_name(_BOT_ALIASES.get(target.lower(), target))
            if name:
                log.info(f"[wd restart {name}] silent={silent}")
                msg = await _say(f"⏳ **{name}** 재시작 중...")
                await loop.run_in_executor(None, restart_bot, name)
                await _say(f"✅ **{name}** 재시작 완료", edit_msg=msg)
            else:
                await ctx.send(f"❌ 봇을 찾을 수 없어요: `{target}`")  # 오류는 silent 무관

    elif action == "stop":
        if target.lower() == "all":
            msg = await _say("⏳ 모든 봇 중지 중...")
            await loop.run_in_executor(None, lambda: [stop_bot(b["name"]) for b in BOTS])
            await _say("⛔ 모든 봇 중지 완료", edit_msg=msg)
        else:
            name = find_bot_name(_BOT_ALIASES.get(target.lower(), target))
            if name:
                msg = await _say(f"⏳ **{name}** 중지 중...")
                await loop.run_in_executor(None, stop_bot, name)
                await _say(f"⛔ **{name}** 중지 완료", edit_msg=msg)
            else:
                await ctx.send(f"❌ 봇을 찾을 수 없어요: `{target}`")

    elif action == "start":
        if target.lower() == "all":
            msg = await _say("⏳ 모든 봇 시작 중...")
            for b in BOTS:
                if b.get("enabled", True):
                    start_bot(b["name"])
            await _say("▶️ 모든 봇 시작 완료", edit_msg=msg)
        else:
            name = find_bot_name(_BOT_ALIASES.get(target.lower(), target))
            if name:
                msg = await _say(f"⏳ **{name}** 시작 중...")
                start_bot(name)
                await _say(f"▶️ **{name}** 시작 완료", edit_msg=msg)
            else:
                await ctx.send(f"❌ 봇을 찾을 수 없어요: `{target}`")

    elif action == "help":
        if target.lower() == "boss":
            await ctx.send(
                "**Boss Bot 명령어** (`!boss [명령어] [인자...]`)\n"
                "`!boss listboss` — 현재 보스 스케줄 목록\n"
                "`!boss startboss [이름] [HH:MM] [주기]` — 보스 스케줄 시작\n"
                "`!boss stopboss [이름]` — 보스 스케줄 중지 (이름 생략 시 전체)\n"
                "`!boss setboss [이름] [시간]` — 보스 주기 변경\n"
                "`!boss history [일수]` — 히스토리 조회 (기본 7일)\n"
                "`!boss stats [일수|all] [상위N|all]` — 참석자 통계 (기본 30일/25명, all 입력 시 제한 해제)\n"
                "`!boss setrefresh [HH:MM]` — 일일 정리 시각 변경\n"
                "`!boss create [이름] [HH:MM] [분]` — 보스 메시지 생성\n"
                "`!boss close [메시지ID]` — 보스 마감\n"
                "`!boss activate [메시지ID] [분]` — 보스 재활성화\n"
                "`!boss extend [메시지ID] [분]` — 마감 시간 연장\n"
                "`!boss reopen [날짜시간] [분]` — 보스 재오픈\n"
                "`!boss rename [기존이름] [새이름]` — 보스 이름 변경\n"
                "`!boss recover [메시지ID]` — 메시지 복구\n"
                "`!boss sync [메시지ID]` — 메시지 동기화\n"
                "`!boss delete [메시지ID]` — 메시지 삭제\n"
                "`!boss listdailyboss` — 매일 고정시간 반복 알림 목록\n"
                "`!boss startdailyboss [이름]` — config daily_bosses 보스 매일 반복 시작\n"
                "`!boss stopdailyboss [이름]` — 매일 반복 알림 중지\n"
                "`!boss attachloot [메시지ID|all] [일수]` — 기존 메시지에 획득 아이템 버튼 부착\n"
                "`!boss fixid` — UID 재매핑\n"
                "`!boss collectmembers` — 서버 멤버 UID↔닉네임 매핑 수집\n"
                "`!boss test` — 봇 동작 테스트\n"
                "`!boss save` — 데이터 저장\n"
                "`!boss refresh` — 데이터 새로고침\n"
                "`!boss restart [silent]` — Boss Bot 재시작 (silent: 디스코드 알림 생략)"
            )
        else:
            await ctx.send(
                "**Watchdog 명령어**\n"
                "`!watchdog status` — 봇 상태 확인\n"
                "`!watchdog restart [봇|all] [silent]` — 재시작 (silent: 알림 생략)\n"
                "`!watchdog stop [봇|all] [silent]` — 중지\n"
                "`!watchdog start [봇|all] [silent]` — 시작\n"
                "`!watchdog help boss` — Boss Bot 명령어 목록\n"
                "`!panel` — 원격 제어 패널 게시 (상태·시작·중지·재시작·로그·업데이트 버튼)\n"
                "\n봇 이름: `boss` `all`"
            )

    else:
        await ctx.send(f"❌ 알 수 없는 명령: `{action}` — `!watchdog help`로 도움말 확인")


@_wdog_bot.command(name="boss")
async def _boss_fwd(ctx, cmd: str = None, *args):
    if not cmd:
        await ctx.send("사용법: `!boss [명령어] [인자...]`")
        return
    status_msg = await ctx.send(f"⏳ Boss Bot — `{cmd}` 처리 중...")
    result = await _send_ipc("boss", cmd, list(args))
    await _reply_ipc(ctx, status_msg, result)


# === 원격 제어 패널 (관리 채널 버튼) ==========================================
# 폰 Discord에서 PC의 봇을 상태 확인·시작·중지·재시작·로그·업데이트한다.
# 버튼은 persistent View(custom_id 고정)라 Watchdog 재시작 후에도 그대로 동작한다.

_PANEL_STATE_PATH = os.path.join(BASE_DIR, "Data", "watchdog_panel.json")
_PANEL_REFRESH_SEC = 60
_PANEL_ADMIN_ROLE_IDS = {int(x) for x in (config.get("admin_role_ids") or []) if str(x).isdigit()}
_PANEL_ADMIN_USER_IDS = {int(x) for x in (config.get("admin_user_ids") or []) if str(x).isdigit()}
_panel_refresh_task = None


def _load_panel_state():
    try:
        with open(_PANEL_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_panel_state(state):
    try:
        from RaidBook_common import write_json_atomic
        os.makedirs(os.path.dirname(_PANEL_STATE_PATH), exist_ok=True)
        write_json_atomic(_PANEL_STATE_PATH, state)
    except Exception as e:
        log.info(f"[원격패널] 상태 저장 실패: {e}")


def _panel_allowed(interaction) -> bool:
    if interaction.channel_id != ADMIN_CHANNEL_ID:
        return False
    member = interaction.user
    if member.id in _PANEL_ADMIN_USER_IDS:
        return True
    guild = interaction.guild
    if guild is not None and member.id == guild.owner_id:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    return any(r.id in _PANEL_ADMIN_ROLE_IDS for r in getattr(member, "roles", []))


def _fmt_uptime(start_time):
    if not start_time:
        return "-"
    sec = int((datetime.now() - start_time).total_seconds())
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}일 {h}시간 {m}분" if d else (f"{h}시간 {m}분" if h else f"{m}분")


def _build_panel_embed():
    try:
        import version as _version
        ver = _version.VERSION
    except Exception:
        ver = "?"
    embed = discord.Embed(title="🖥️ RaidBook 원격 제어", color=0x5865F2)
    any_down = False
    with bot_states_lock:
        for b in BOTS:
            name = b["name"]
            state = bot_states.get(name, {})
            process = state.get("process")
            if state.get("stopped", True):
                status = "🔴 중지" + (" (빠른 재시작 제한)" if state.get("rapid_limit") else "")
                any_down = True
                detail = ""
            elif process and process.poll() is None:
                status = "🟢 실행중"
                detail = (f"\nPID {process.pid} · 가동 {_fmt_uptime(state.get('start_time'))}"
                          f" · 재시작 {state.get('restart_count', 0)}회")
            else:
                status = "🟡 재시작 대기"
                any_down = True
                detail = ""
            embed.add_field(name=name, value=status + detail, inline=False)
    if any_down:
        embed.color = 0xED4245
    embed.add_field(name="PC", value=f"{socket.gethostname()} · v{ver}", inline=False)
    embed.set_footer(text=f"마지막 갱신 {datetime.now().strftime('%m-%d %H:%M:%S')} · {_PANEL_REFRESH_SEC}초마다 자동 갱신")
    return embed


async def _refresh_panel_message(message=None):
    """저장된 패널 메시지를 현재 상태로 갱신. 삭제됐으면 저장값을 비운다."""
    try:
        if message is None:
            mid = int(_load_panel_state().get("message_id") or 0)
            if not mid:
                return
            ch = _wdog_bot.get_channel(ADMIN_CHANNEL_ID) or await _wdog_bot.fetch_channel(ADMIN_CHANNEL_ID)
            message = await ch.fetch_message(mid)
        await message.edit(embed=_build_panel_embed(), view=RemotePanelView())
    except discord.NotFound:
        _save_panel_state({})
    except Exception as e:
        log.info(f"[원격패널] 갱신 실패 (무시): {e}")


async def _panel_refresh_loop():
    while True:
        await asyncio.sleep(_PANEL_REFRESH_SEC)
        await _refresh_panel_message()


async def _panel_bot_action(interaction, action):
    loop = asyncio.get_event_loop()
    names = [b["name"] for b in BOTS]
    log.info(f"[원격패널] {interaction.user} → {action} {names}")
    if action == "start":
        for b in BOTS:
            if b.get("enabled", True):
                start_bot(b["name"])
        done = "▶️ 시작했습니다."
    elif action == "stop":
        await loop.run_in_executor(None, lambda: [stop_bot(n) for n in names])
        done = "⛔ 중지했습니다."
    else:
        await loop.run_in_executor(None, lambda: [restart_bot(n) for n in names])
        done = "🔄 재시작했습니다."
    await asyncio.sleep(1.5)
    await _refresh_panel_message(interaction.message)
    await interaction.followup.send(f"{done} (요청: {interaction.user.display_name})", ephemeral=True)


class _UpdateConfirmView(discord.ui.View):
    def __init__(self, release_info, download_url):
        super().__init__(timeout=300)
        self.release_info = release_info
        self.download_url = download_url

    @discord.ui.button(label="지금 설치", emoji="⬆️", style=discord.ButtonStyle.success)
    async def install(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _panel_allowed(interaction):
            await interaction.response.send_message("⛔ 권한이 없습니다.", ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(content="⏳ 다운로드 중...", view=self)
        import RaidBook_updater as _updater
        path = await asyncio.get_event_loop().run_in_executor(
            None, _updater.UpdateDownloader().download, self.download_url)
        if not path:
            await interaction.edit_original_response(content="❌ 다운로드 실패 — 토큰(Config/github_token.txt)과 네트워크를 확인하세요.", view=None)
            return
        ver = self.release_info.get("version", "?")
        log.info(f"[원격패널] v{ver} 무인 설치 시작: {path} (요청: {interaction.user})")
        await interaction.edit_original_response(content=f"🚀 v{ver} 설치 시작 — 봇이 잠시 꺼졌다가 1~2분 안에 자동으로 다시 켜집니다.", view=None)
        try:
            ch = interaction.channel
            await ch.send(f"⬆️ **v{ver} 업데이트 설치 중** (요청: {interaction.user.display_name}) — 완료되면 'Watchdog 시작됨' 메시지가 올라옵니다.")
        except Exception:
            pass
        await asyncio.sleep(1)
        # 인스톨러가 실행 중인 RaidBook을 종료 → 파일 교체 → 자동 재실행한다.
        subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class RemotePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _panel_allowed(interaction):
            return True
        await interaction.response.send_message("⛔ 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return False

    @discord.ui.button(label="상태", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="wd_panel_status", row=0)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_build_panel_embed(), view=self)

    @discord.ui.button(label="시작", emoji="▶️", style=discord.ButtonStyle.success, custom_id="wd_panel_start", row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _panel_bot_action(interaction, "start")

    @discord.ui.button(label="재시작", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="wd_panel_restart", row=0)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _panel_bot_action(interaction, "restart")

    @discord.ui.button(label="중지", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="wd_panel_stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _panel_bot_action(interaction, "stop")

    @discord.ui.button(label="최근 로그", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="wd_panel_logs", row=1)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        parts = []
        for b in BOTS:
            lines = recent_output(b["name"], 25)
            body = "\n".join(lines) if lines else "(출력 없음)"
            parts.append(f"**{b['name']}**\n```\n{body[-1700:]}\n```")
        await interaction.response.send_message("\n".join(parts)[:1990], ephemeral=True)

    @discord.ui.button(label="업데이트", emoji="⬆️", style=discord.ButtonStyle.secondary, custom_id="wd_panel_update", row=1)
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import RaidBook_updater as _updater
            import version as _version
        except ImportError:
            await interaction.followup.send("❌ 업데이트 모듈이 없습니다.", ephemeral=True)
            return
        checker = _updater.UpdateChecker()
        has_update, info = await asyncio.get_event_loop().run_in_executor(None, checker.check_for_updates)
        if not has_update or not info:
            await interaction.followup.send(f"✅ 최신 버전입니다 (v{_version.VERSION}).", ephemeral=True)
            return
        url = checker.get_download_url(info)
        notes = (info.get("body") or "").strip()[:800]
        text = f"🎉 새 버전 **v{info['version']}** (현재 v{_version.VERSION})" + (f"\n>>> {notes}" if notes else "")
        if not is_frozen():
            await interaction.followup.send(text + "\n\n소스 실행 모드라 자동 설치는 불가합니다 — 설치판에서 사용하세요.", ephemeral=True)
            return
        if not url:
            await interaction.followup.send(text + "\n\n❌ 릴리즈에 설치 파일이 없습니다.", ephemeral=True)
            return
        await interaction.followup.send(text, view=_UpdateConfirmView(info, url), ephemeral=True)


async def _start_panel_services():
    global _panel_refresh_task
    _wdog_bot.add_view(RemotePanelView())
    await _refresh_panel_message()
    if _panel_refresh_task is None or _panel_refresh_task.done():
        _panel_refresh_task = asyncio.create_task(_panel_refresh_loop())


@_wdog_bot.command(name="panel", aliases=["패널", "원격"])
async def _panel_cmd(ctx):
    """관리 채널에 원격 제어 패널을 게시 (기존 패널은 삭제)."""
    old = int(_load_panel_state().get("message_id") or 0)
    if old:
        try:
            await (await ctx.channel.fetch_message(old)).delete()
        except Exception:
            pass
    msg = await ctx.send(embed=_build_panel_embed(), view=RemotePanelView())
    _save_panel_state({"message_id": msg.id})
    try:
        await msg.pin()
    except Exception:
        pass
    log.info(f"[원격패널] 게시: message_id={msg.id}")


def _raidbook_bot_ids() -> dict:
    """각 봇 config의 토큰 첫 세그먼트(base64)에서 봇 유저 ID를 유도. {이름: id}"""
    import base64 as _b64
    ids = {}
    for name in ("watchdog", "boss", "combat", "accountant", "tts"):
        try:
            with open(os.path.join(BASE_DIR, "Config", f"{name}_config.json"), encoding="utf-8") as f:
                tok = (json.load(f).get("token") or "").strip()
            if not tok:
                continue
            seg = tok.split(".")[0]
            ids[name] = int(_b64.b64decode(seg + "=" * (-len(seg) % 4)).decode())
        except Exception:
            continue
    return ids


@_wdog_bot.command(name="purgebot")
async def _purgebot(ctx, target: str = "", days: int = 0, confirm: str = ""):
    """RaidBook 봇들이 작성한 메시지 일괄 삭제.

    사용법: !purgebot <here|채널ID|all> [일수] [confirm]
      - here: 현재 채널 / all: 서버 전체 텍스트 채널
      - 일수: 최근 N일치만 (0=전체 기간)
      - confirm 없이 실행하면 삭제하지 않고 대상 개수만 미리보기
    """
    if not target:
        await ctx.send("사용법: `!purgebot <here|채널ID|all> [일수] [confirm]`\n"
                       "confirm 없이 실행하면 개수만 미리 확인합니다.")
        return

    bot_ids = _raidbook_bot_ids()
    if not bot_ids:
        await ctx.send("❌ 봇 토큰에서 봇 ID를 확인할 수 없습니다.")
        return
    id_set = set(bot_ids.values())
    do_delete = str(confirm).lower() == "confirm"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days > 0 else None

    # 대상 채널 결정
    if target.lower() == "here":
        channels = [ctx.channel]
    elif target.lower() == "all":
        channels = [c for c in ctx.guild.text_channels]
    else:
        ch = _wdog_bot.get_channel(int(re.sub(r"\D", "", target) or 0))
        if ch is None:
            await ctx.send("❌ 채널을 찾을 수 없습니다.")
            return
        channels = [ch]

    status = await ctx.send(f"⏳ {'삭제' if do_delete else '조회'} 중... (채널 {len(channels)}개)")
    bulk_limit = datetime.now(timezone.utc) - timedelta(days=13, hours=12)
    total_found = total_deleted = 0
    lines = []
    for ch in channels:
        try:
            targets = []
            async for msg in ch.history(limit=None, after=cutoff, oldest_first=False):
                if msg.author.id in id_set:
                    targets.append(msg)
            if not targets:
                continue
            total_found += len(targets)
            deleted = 0
            if do_delete:
                recent = [m for m in targets if m.created_at > bulk_limit]
                old = [m for m in targets if m.created_at <= bulk_limit]
                for i in range(0, len(recent), 100):
                    chunk = recent[i:i + 100]
                    try:
                        await ch.delete_messages(chunk)
                        deleted += len(chunk)
                    except Exception:
                        old.extend(chunk)  # 벌크 실패 시 개별 삭제로 폴백
                    await asyncio.sleep(1.0)
                for m in old:
                    try:
                        await m.delete()
                        deleted += 1
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)
                total_deleted += deleted
            lines.append(f"  <#{ch.id}>: {len(targets)}건" + (f" → {deleted}건 삭제" if do_delete else ""))
        except discord.Forbidden:
            lines.append(f"  <#{ch.id}>: ⚠ 권한 없음")
        except Exception as e:
            lines.append(f"  <#{ch.id}>: ⚠ 오류 {e}")

    period = f"최근 {days}일" if days > 0 else "전체 기간"
    head = (f"🧹 **봇 메시지 삭제 완료** — {total_deleted}/{total_found}건 ({period})"
            if do_delete else
            f"🔎 **봇 메시지 미리보기** — 총 {total_found}건 ({period})\n"
            f"삭제하려면 `!purgebot {target} {days} confirm`\n"
            f"⚠️ 진행 중인 참석/경매/입찰 메시지도 함께 삭제되어 봇 상태가 깨질 수 있습니다.")
    body = "\n".join(lines[-25:]) if lines else "  대상 없음"
    log.info(f"[purgebot] target={target} days={days} confirm={do_delete} found={total_found} deleted={total_deleted}")
    try:
        await status.edit(content=f"{head}\n{body}"[:1990])
    except Exception:
        await ctx.send(f"{head}\n{body}"[:1990])


def _run_discord_watchdog():
    global _discord_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _discord_loop = loop
    try:
        loop.run_until_complete(_wdog_bot.start(WATCHDOG_TOKEN))
    except Exception as e:
        log.info(f"[Watchdog Discord] 오류: {e}")
    finally:
        _discord_loop = None
        loop.close()


def restart_discord_bot():
    """Discord 봇을 안전하게 종료하고 새 토큰/채널로 재시작 (설정 변경 시 GUI에서 사용)."""
    global _wdog_ready_announced, _wdog_slash_synced
    loop = _discord_loop
    if loop and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_wdog_bot.close(), loop).result(timeout=8)
        except Exception as e:
            log.info(f"[Watchdog Discord] 종료 중 오류: {e}")

    # Bot.__init__을 다시 호출하면 decorator로 등록된 명령어가 사라질 수 있다.
    # 같은 Bot 객체를 재사용해 명령 테이블을 유지한다.
    _wdog_ready_announced = False
    _wdog_slash_synced = False
    time.sleep(2)
    if WATCHDOG_TOKEN and (ADMIN_CHANNEL_ID or config.get("auto_create_channels", True)):
        t = threading.Thread(target=_run_discord_watchdog, daemon=True)
        t.start()
        log.info("[Watchdog Discord] Discord 봇 재시작됨")


def sync_base_dir():
    """모든 config의 base_dir이 writable runtime 위치와 다르면 자동으로 수정한다."""
    config_files = {
        "watchdog": CONFIG_PATH,
        "boss": get_config_path("boss_config.json"),
    }
    updated = []
    for name, path in config_files.items():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            old_dir = cfg.get("base_dir", "")
            if old_dir and os.path.normcase(old_dir) != os.path.normcase(_RUNTIME_DIR):
                cfg["base_dir"] = _RUNTIME_DIR
                from RaidBook_common import write_json_atomic
                write_json_atomic(path, cfg)
                updated.append(f"{name}: {old_dir!r} → {_RUNTIME_DIR!r}")
        except Exception as e:
            log.info(f"[base_dir 동기화 실패] {name}: {e}")
    if updated:
        log.info("[base_dir 동기화] 경로 불일치 감지 — 아래 config 수정됨:")
        for line in updated:
            log.info(f"  {line}")
    else:
        log.info(f"[base_dir 동기화] 모든 config 경로 정상: {_RUNTIME_DIR!r}")


def start_discord_bot_thread():
    """Watchdog Discord 봇을 백그라운드 스레드로 시작 (token/channel 설정된 경우만)."""
    # 관리 채널이 비어 있어도 시작 — on_ready에서 자동 생성 (auto_create_channels)
    if WATCHDOG_TOKEN and (ADMIN_CHANNEL_ID or config.get("auto_create_channels", True)):
        t = threading.Thread(target=_run_discord_watchdog, daemon=True)
        t.start()
        log.info(f"[Watchdog Discord] 봇 시작 중... (관리 채널: {ADMIN_CHANNEL_ID or '자동 생성'})")
        return t
    log.info("[Watchdog Discord] token 또는 admin_channel_id 미설정 — Discord 봇 비활성")
    return None


def start_all_enabled_bots():
    """config의 bots 목록 중 enabled=True인 항목을 모두 시작."""
    _init_job_object()   # 봇 spawn 전에 Job Object 준비 (고아 프로세스 방지)
    for bot_config in BOTS:
        if bot_config.get("enabled", True):
            start_bot(bot_config["name"])


def main():
    """콘솔 모드 진입점: python RaidBook_Watchdog.py"""
    log.info("=" * 50)
    log.info("RaidBook Watchdog 시작")
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

    sync_base_dir()

    start_network_monitor()

    # Watchdog Discord 봇 시작
    start_discord_bot_thread()

    # 봇 시작
    start_all_enabled_bots()

    # 명령어 입력 루프 (메인 스레드)
    command_loop()


if __name__ == "__main__":
    main()
