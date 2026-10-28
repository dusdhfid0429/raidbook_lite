# -*- coding: utf-8 -*-
"""음악 봇 공용 로직 — 외부 도구(yt-dlp·FFmpeg·Deno) 위치/설치, 유튜브 링크 해석, 대기열.

Discord 의존 없이 테스트할 수 있도록 RaidBook_Music_Bot.py에서 분리했다.
- yt-dlp는 파이썬 패키지를 exe에 묶지 않고 단독 실행 파일(yt-dlp.exe)을 Tools 폴더에 받아 쓴다.
  유튜브 변경으로 yt-dlp가 자주 깨지므로 `yt-dlp -U` 자동 업데이트가 가능해야 하기 때문.
- 최신 yt-dlp는 유튜브 해석에 JS 런타임(Deno)을 쓰므로 Windows에서는 deno.exe도 함께 받는다.
"""
import asyncio
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass, field

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

# 재생 중 스트림이 끊겨도 ffmpeg가 다시 연결하도록
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn"

_URL_RE = re.compile(r"^https?://", re.I)
_YT_HOST_RE = re.compile(r"^https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)


def is_url(text: str) -> bool:
    return bool(_URL_RE.match((text or "").strip()))


def is_youtube_url(text: str) -> bool:
    return bool(_YT_HOST_RE.match((text or "").strip()))


def format_duration(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "LIVE"
    if s <= 0:
        return "LIVE"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Track:
    title: str
    url: str                 # 유튜브 페이지 주소 (스트림 주소는 재생 직전에 새로 받는다 — 몇 시간 뒤 만료)
    duration: float = 0
    requester_id: int = 0
    requester_name: str = ""

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)


@dataclass
class MusicQueue:
    max_size: int = 50
    items: deque = field(default_factory=deque)

    def add(self, tracks) -> int:
        """들어갈 수 있는 만큼 추가하고 추가한 개수를 돌려준다."""
        added = 0
        for t in tracks:
            if len(self.items) >= self.max_size:
                break
            self.items.append(t)
            added += 1
        return added

    def pop(self):
        return self.items.popleft() if self.items else None

    def remove(self, index: int):
        """1부터 세는 번호로 삭제. 없으면 None."""
        if 1 <= index <= len(self.items):
            t = self.items[index - 1]
            del self.items[index - 1]
            return t
        return None

    def clear(self):
        self.items.clear()

    def __len__(self):
        return len(self.items)


# ── 외부 도구 ──────────────────────────────────────────────────────────────
def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def find_ffmpeg(app_dir: str, tools_dir: str) -> str:
    """설치 폴더 ffmpeg.exe → Tools → imageio-ffmpeg 패키지 → PATH 순으로 찾는다."""
    for d in (app_dir, tools_dir):
        p = os.path.join(d, _exe("ffmpeg"))
        if os.path.isfile(p):
            return p
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


def find_deno(tools_dir: str) -> str:
    p = os.path.join(tools_dir, _exe("deno"))
    if os.path.isfile(p):
        return p
    return shutil.which("deno") or ""


def ytdlp_command(tools_dir: str, frozen: bool) -> list:
    """yt-dlp 실행 명령. Tools/yt-dlp(.exe) → PATH → (소스 실행 시) python -m yt_dlp."""
    p = os.path.join(tools_dir, _exe("yt-dlp"))
    if os.path.isfile(p):
        return [p]
    w = shutil.which("yt-dlp")
    if w:
        return [w]
    if not frozen:
        try:
            import importlib.util
            if importlib.util.find_spec("yt_dlp"):
                return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            pass
    return []


def _download(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "RaidBook-MusicBot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _write_bytes(path: str, data: bytes):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def install_tools(tools_dir: str, log=None) -> list:
    """Windows에서 yt-dlp.exe / deno.exe가 없으면 GitHub 최신 릴리즈에서 받는다 (동기 함수 — 스레드에서 호출).
    받은 도구 이름 목록을 돌려준다."""
    done = []
    if os.name != "nt":
        return done
    os.makedirs(tools_dir, exist_ok=True)
    ytdlp = os.path.join(tools_dir, "yt-dlp.exe")
    if not os.path.isfile(ytdlp):
        if log:
            log.info("[도구] yt-dlp.exe 다운로드 중...")
        _write_bytes(ytdlp, _download(YTDLP_URL))
        done.append("yt-dlp")
    deno = os.path.join(tools_dir, "deno.exe")
    if not os.path.isfile(deno):
        try:
            if log:
                log.info("[도구] deno.exe 다운로드 중...")
            with zipfile.ZipFile(io.BytesIO(_download(DENO_URL, timeout=300))) as z:
                _write_bytes(deno, z.read("deno.exe"))
            done.append("deno")
        except Exception as e:   # Deno가 없어도 yt-dlp는 제한 모드로 동작하므로 계속 진행
            if log:
                log.warning(f"[도구] deno 다운로드 실패 (계속 진행): {type(e).__name__}: {e}")
    return done


def ytdlp_base_args(deno_path: str = "") -> list:
    # --encoding utf-8: Windows에서 제목 등 한글 출력이 CP949로 나와 깨지는 것 방지
    args = ["--no-warnings", "--no-progress", "--no-color", "--ignore-config", "--encoding", "utf-8"]
    if deno_path:
        args += ["--js-runtimes", f"deno:{deno_path}"]
    return args


async def run_ytdlp(cmd: list, args: list, timeout: float = 60) -> tuple:
    """yt-dlp 실행 → (returncode, stdout, stderr)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW — 콘솔 창 깜빡임 방지
    proc = await asyncio.create_subprocess_exec(
        *cmd, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "시간 초과"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _entry_url(entry: dict) -> str:
    url = entry.get("webpage_url") or entry.get("url") or ""
    if url and is_url(url):
        return url
    vid = entry.get("id")
    return f"https://www.youtube.com/watch?v={vid}" if vid else ""


def tracks_from_info(info: dict, requester_id: int = 0, requester_name: str = "", limit: int = 50) -> list:
    """yt-dlp -J 결과(단일 영상/재생목록/검색 결과)를 Track 목록으로."""
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    tracks = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        url = _entry_url(e)
        if not url:
            continue
        tracks.append(Track(
            title=str(e.get("title") or url),
            url=url,
            duration=e.get("duration") or 0,
            requester_id=requester_id,
            requester_name=requester_name,
        ))
        if len(tracks) >= limit:
            break
    return tracks


def short_error(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errs = [ln for ln in lines if ln.startswith("ERROR")]
    msg = (errs or lines or ["알 수 없는 오류"])[-1]
    return msg[:300]


async def lookup(cmd: list, query: str, deno_path: str = "", requester_id: int = 0,
                 requester_name: str = "", limit: int = 50) -> list:
    """링크(영상/재생목록) 또는 검색어 → Track 목록. 실패 시 RuntimeError."""
    q = query.strip()
    target = q if is_url(q) else f"ytsearch1:{q}"
    args = ytdlp_base_args(deno_path) + ["--flat-playlist", "-J", "--playlist-end", str(limit), target]
    code, out, err = await run_ytdlp(cmd, args, timeout=90)
    if code != 0:
        raise RuntimeError(short_error(err))
    try:
        info = json.loads(out)
    except Exception:
        raise RuntimeError("yt-dlp 결과를 읽지 못했습니다")
    return tracks_from_info(info, requester_id, requester_name, limit)


async def stream_url(cmd: list, page_url: str, deno_path: str = "") -> str:
    """재생 직전 오디오 스트림 주소를 받는다."""
    args = ytdlp_base_args(deno_path) + ["-f", "bestaudio/best", "-g", "--no-playlist", page_url]
    code, out, err = await run_ytdlp(cmd, args, timeout=60)
    urls = [ln.strip() for ln in out.splitlines() if is_url(ln.strip())]
    if code != 0 or not urls:
        raise RuntimeError(short_error(err))
    return urls[0]


def ytdlp_pipe_args(page_url: str, deno_path: str = "") -> list:
    """yt-dlp가 직접 받아 표준출력으로 흘려보내는 인자.
    스트림 주소를 ffmpeg에 넘기면 유튜브가 403으로 막는 경우가 많아(클라이언트 헤더·토큰 불일치),
    받기는 yt-dlp가 하고 ffmpeg는 파이프로 디코딩만 한다."""
    return ytdlp_base_args(deno_path) + ["-f", "bestaudio/best", "--no-playlist", "--no-part", "-q",
                                         "-o", "-", page_url]


async def self_update(cmd: list) -> str:
    """단독 실행 파일일 때만 yt-dlp -U. 결과 한 줄."""
    if len(cmd) != 1:
        return ""
    code, out, err = await run_ytdlp(cmd, ["-U"], timeout=180)
    lines = [ln.strip() for ln in (out + "\n" + err).splitlines() if ln.strip()]
    return lines[-1] if lines else f"code={code}"


# ── 슬래시 명령 공존 (보스봇과 같은 토큰을 쓸 때) ──────────────────────────
# 같은 토큰 = 같은 Discord 애플리케이션이라 서버의 슬래시 명령 목록도 하나다.
# tree.sync()는 목록 전체를 덮어쓰므로, 음악 봇은 자기 명령만 하나씩 등록(upsert)하고
# 보스봇은 동기화할 때 아래 이름의 기존 명령을 지우지 않고 되살린다.
MUSIC_COMMAND_NAMES = frozenset({"재생", "건너뛰기", "일시정지", "정지", "대기열", "현재곡", "삭제", "볼륨"})

_PAYLOAD_KEYS = ("type", "name", "description", "name_localizations", "description_localizations",
                 "options", "default_member_permissions", "nsfw")


def clean_command_payload(payload: dict) -> dict:
    """fetch한 명령(AppCommand.to_dict) → 등록용 payload (id·None 값 제거)."""
    return {k: payload[k] for k in _PAYLOAD_KEYS if payload.get(k) is not None}


async def sync_guild_keeping(tree, guild, keep_names=MUSIC_COMMAND_NAMES) -> list:
    """tree.sync(guild)와 같지만, 다른 프로그램이 같은 토큰으로 등록한 keep_names 명령은 남긴다."""
    try:
        before = await tree.fetch_commands(guild=guild)
    except Exception:
        before = []
    synced = await tree.sync(guild=guild)
    own = {c.name for c in synced}
    keep = [c for c in before if c.name in keep_names and c.name not in own]
    app_id = tree.client.application_id
    for c in keep:
        await tree.client.http.upsert_guild_command(app_id, guild.id, clean_command_payload(c.to_dict()))
    return synced
