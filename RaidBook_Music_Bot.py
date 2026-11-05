# -*- coding: utf-8 -*-
"""RaidBook Music Bot — 유튜브 링크를 음성채널에서 스트리밍 재생하는 별도 봇 (자체 토큰).

/재생 <유튜브 링크 또는 검색어> → 명령을 입력한 사람이 있는 음성채널에 들어가 재생.
/건너뛰기 /일시정지 /정지 /대기열 /삭제 /볼륨 /현재곡, 재생 메시지의 ⏯ ⏭ ⏹ 📜 버튼.
설정: Config/music_config.json — token이 비어 있으면 보스봇 토큰(boss_config.json)을 같이 쓴다.
같은 토큰이면 슬래시 명령을 덮어쓰지 않도록 자기 명령만 하나씩 등록한다 (RaidBook_music.MUSIC_COMMAND_NAMES).

동시 재생: 봇 계정 하나는 서버당 음성채널 하나에만 들어갈 수 있어서, extra_tokens에 넣은 추가 봇들을
같은 프로세스에서 음성 전용 클라이언트로 띄운다. 명령·버튼·안내 메시지는 메인 봇이 받고 보내며,
/재생 때 그 사람의 음성채널에 이미 있는 봇 → 비어 있는 봇 순으로 배정한다.
대기열·재생곡·볼륨·안내 메시지는 (봇, 서버)마다 따로라 채널끼리 섞이지 않는다.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands

import RaidBook_music as rbm
from RaidBook_storage import DailyFileHandler, write_json_atomic
from config_helper import (
    ensure_config_from_template,
    ensure_runtime_tree,
    get_app_dir,
    get_config_path,
    get_runtime_dir,
    is_frozen,
)

# 설치판(PyInstaller)은 PYTHONIOENCODING/PYTHONUTF8 환경변수를 무시해 stdout이 CP949로 나가고,
# Watchdog이 UTF-8로 읽으면서 로그 창 한글이 깨진다 → 출력 스트림을 직접 UTF-8로 고정 (보스봇과 동일).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if "--selftest" in sys.argv:
    # 빌드 검증용 (build.ps1): 설치판 exe에 음성 라이브러리가 들어갔는지 확인하고 바로 종료
    _bad = []
    for _mod in ("nacl.secret", "nacl.utils", "davey"):
        try:
            __import__(_mod)
        except Exception as _e:
            _bad.append(f"{_mod}: {type(_e).__name__}: {_e}")
    try:
        import davey as _davey
        # davey 0.2+는 키 전환 API가 바뀌어 discord.py 2.7.1과 쓰면 소리가 나가지 않는다 (requirements.txt 참고)
        if hasattr(_davey.DaveSession, "prepare_transition") and discord.__version__ <= "2.7.1":
            _bad.append(f"davey: discord.py {discord.__version__}와 호환되지 않는 버전 (0.1.x 필요)")
    except Exception:
        pass
    try:
        discord.opus._load_default()
        if not discord.opus.is_loaded():
            _bad.append("opus: 로드 안 됨")
    except Exception as _e:
        _bad.append(f"opus: {type(_e).__name__}: {_e}")
    print("selftest " + ("OK" if not _bad else "FAIL\n" + "\n".join(_bad)))
    sys.exit(1 if _bad else 0)

_SCRIPT_DIR = get_app_dir()
_RUNTIME_DIR = get_runtime_dir()
ensure_runtime_tree()
CONFIG_PATH = get_config_path("music_config.json")

if not ensure_config_from_template(CONFIG_PATH):
    print(f"❌ 설정 파일을 찾을 수 없습니다: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(1)


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


config = _load_config()
BASE_DIR = config.get("base_dir") or _RUNTIME_DIR
LOG_DIR = os.path.join(BASE_DIR, "Log")
TOOLS_DIR = os.path.join(BASE_DIR, "Tools")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("MusicBot")
log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_console = logging.StreamHandler()
_console.setFormatter(_formatter)
log.addHandler(_console)
_file = DailyFileHandler(LOG_DIR, "RaidBook_Music_Bot")
_file.setFormatter(_formatter)
log.addHandler(_file)
# discord.py 자체 경고(음성 연결·E2EE(DAVE) 실패 등)도 같은 로그에 남긴다 — 소리 안 남 같은 문제 진단용
_dlog = logging.getLogger("discord")
_dlog.setLevel(logging.WARNING)
_dlog.addHandler(_console)
_dlog.addHandler(_file)


def _int_set(values) -> set:
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return out


# v1.0.17 설치본이 만든 예전 기본 채널 → 새 기본 채널로 1회 교체
_OLD_DEFAULT_CHANNEL, _NEW_DEFAULT_CHANNEL = 1565039317695467561, 1565041141890547813
if [str(c) for c in config.get("command_channel_ids") or []] == [str(_OLD_DEFAULT_CHANNEL)]:
    config["command_channel_ids"] = [_NEW_DEFAULT_CHANNEL]
    try:
        write_json_atomic(CONFIG_PATH, config)
        log.info(f"[설정] 음악 명령 채널 → {_NEW_DEFAULT_CHANNEL}")
    except Exception as e:
        log.warning(f"[설정] 음악 명령 채널 저장 실패 (이번 실행만 적용): {e}")

# 비어 있으면 모든 채널에서 명령 허용
COMMAND_CHANNEL_IDS = _int_set(config.get("command_channel_ids"))
# 음성채널 채팅에서도 음악 명령 허용 (여러 채널 동시 재생용, 동훈님 요청 2026-11-04)
ALLOW_VOICE_CHAT = bool(config.get("allow_voice_chat", True))
# 같은 음성채널에 없어도 건너뛰기/정지 등을 할 수 있는 역할 (서버 관리 권한자는 항상 가능)
CONTROL_ROLE_IDS = _int_set(config.get("control_role_ids"))
DEFAULT_VOLUME = max(1, min(100, int(config.get("default_volume", 50) or 50)))
# 예전 기본값(대기열 50곡·재생목록 30곡)으로 만들어진 설정은 새 기본값 10곡으로 1회 교체 (동훈님 요청 2026-10-30)
_queue_migrated = False
for _key, _old in (("max_queue", 50), ("max_playlist", 30)):
    if config.get(_key) == _old:
        config[_key] = 10
        _queue_migrated = True
if _queue_migrated:
    try:
        write_json_atomic(CONFIG_PATH, config)
        log.info("[설정] 대기열·재생목록 최대 곡 수 → 10곡")
    except Exception as e:
        log.warning(f"[설정] 대기열 최대 곡 수 저장 실패 (이번 실행만 적용): {e}")
MAX_QUEUE = max(1, int(config.get("max_queue", 10) or 10))
MAX_PLAYLIST = max(1, int(config.get("max_playlist", 10) or 10))
IDLE_DISCONNECT = max(30, int(config.get("idle_disconnect_seconds", 300) or 300))
# 듣는 사람이 아무도 없을 때 일시정지 후 이 시간이 지나면 재생 종료·퇴장 (그 사이 누가 들어오면 이어서 재생)
EMPTY_DISCONNECT = max(10, int(config.get("empty_disconnect_seconds", 60) or 60))
YOUTUBE_ONLY = bool(config.get("youtube_only", True))
AUTO_UPDATE = bool(config.get("auto_update_ytdlp", True))
# 0이면 음성채널 비트레이트에 맞춤 (최소 128kbps)
BITRATE_KBPS = int(config.get("bitrate_kbps", 0) or 0)

intents = discord.Intents.default()   # guilds + voice_states 만 필요 (권한 인텐트 불필요)
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
SHARED_TOKEN = False   # 보스봇과 같은 토큰으로 실행 중인지 (시작 시 결정)


async def _tree_on_error(interaction, error):
    # 같은 토큰이면 보스봇 명령의 상호작용도 이 프로세스로 오므로 조용히 무시
    if isinstance(error, app_commands.CommandNotFound):
        return
    log.warning(f"[명령 오류] {getattr(interaction.command, 'name', '?')}: {type(error).__name__}: {error}")


bot.tree.on_error = _tree_on_error


# ── 서버별 재생 상태 ──────────────────────────────────────────────────────
class GuildPlayer:
    """봇 하나가 서버 하나에서 재생하는 상태. 봇마다 따로라 대기열·재생곡이 섞이지 않는다."""

    def __init__(self, client: discord.Client, guild_id: int):
        self.client = client
        self.guild_id = guild_id
        self.queue = rbm.MusicQueue(max_size=MAX_QUEUE)
        self.current = None              # 재생 중 Track
        self.volume = DEFAULT_VOLUME / 100
        self.text_channel_id = 0         # 재생 안내 메시지를 올릴 채널 (/재생 입력 채널)
        self.np_message = None           # 현재 재생 메시지
        self.lock = asyncio.Lock()
        self.idle_task = None
        self.idle_empty = False          # 진행 중인 퇴장 타이머가 '빈 채널' 타이머인지
        self.started_at = 0.0

    def voice(self):
        g = self.client.get_guild(self.guild_id)
        return g.voice_client if g else None

    def connected(self) -> bool:
        vc = self.voice()
        return bool(vc and vc.is_connected() and vc.channel)

    def busy(self) -> bool:
        """재생 중·일시정지·대기곡이 있으면 다른 채널로 데려가면 안 된다."""
        vc = self.voice()
        return bool(self.current or len(self.queue) or (vc and (vc.is_playing() or vc.is_paused())))


players: dict = {}             # (id(client), guild_id) → GuildPlayer
workers: list = []             # 추가 음악봇 (음성 전용 discord.Client)
_assign_locks: dict = {}       # guild_id → 봇 배정 잠금 (동시에 /재생 해도 같은 봇이 두 채널에 배정되지 않게)


def get_player(client: discord.Client, guild_id: int) -> GuildPlayer:
    key = (id(client), guild_id)
    p = players.get(key)
    if p is None:
        p = players[key] = GuildPlayer(client, guild_id)
    return p


def _clients() -> list:
    """재생에 쓸 수 있는 봇. 추가 음악봇(extra_tokens)이 있으면 그 봇들만 음성에 들어가고,
    보스봇 토큰을 쓰는 메인 봇은 명령·패널만 맡는다 (동훈님 요청 2026-11-05). 추가 봇이 없으면 메인 봇이 재생."""
    if workers:
        return [w for w in workers if w.is_ready()]
    return [bot]


def _guild_players(guild_id: int) -> list:
    return [p for p in players.values() if p.guild_id == guild_id]


def _player_in_channel(guild_id: int, channel_id: int):
    for c in _clients():
        p = get_player(c, guild_id)
        if p.connected() and p.voice().channel.id == channel_id:
            return p
    return None


def _missing_voice_perms(client: discord.Client, channel_id: int) -> list:
    """그 봇이 이 음성채널에서 못 하는 것 (보기·연결·말하기). 빈 목록이면 OK."""
    ch = client.get_channel(channel_id)
    if ch is None:
        return ["채널 보기"]
    perms = ch.permissions_for(ch.guild.me)
    return [name for name, ok in (("채널 보기", perms.view_channel), ("연결", perms.connect),
                                  ("말하기", perms.speak)) if not ok]


def _pick_player(guild_id: int, channel_id: int, exclude=()):
    """그 음성채널에 이미 있는 봇 → 음성에 없는 봇 → 놀고 있는 봇 순으로 고른다. 없으면 None.
    그 채널 권한(보기·연결·말하기)이 없는 봇과 exclude(이번에 연결 실패한 봇)는 건너뛴다."""
    cands = []
    for c in _clients():
        if c.get_guild(guild_id) is None or c in exclude:   # 이 서버에 초대되지 않은 봇
            continue
        if _missing_voice_perms(c, channel_id):
            continue
        p = get_player(c, guild_id)
        cands.append((p, p.voice().channel.id if p.connected() else None, p.busy()))
    return rbm.pick_bot(cands, channel_id)


def _find_player(interaction: discord.Interaction):
    """조작 대상: 누른 재생 패널의 봇 → 입력한 사람의 음성채널 봇 → 입력한 음성채널 채팅의 봇 → 하나뿐인 재생."""
    gid = interaction.guild.id
    msg = getattr(interaction, "message", None)
    if msg is not None:
        for p in _guild_players(gid):
            if p.np_message and p.np_message.id == msg.id:
                return p
    member = interaction.user
    if getattr(member, "voice", None) and member.voice.channel:
        p = _player_in_channel(gid, member.voice.channel.id)
        if p:
            return p
    if isinstance(interaction.channel, (discord.VoiceChannel, discord.StageChannel)):
        p = _player_in_channel(gid, interaction.channel.id)
        if p:
            return p
    active = [p for p in _guild_players(gid) if p.connected()]
    return active[0] if len(active) == 1 else None


def _tools():
    cmd = rbm.ytdlp_command(TOOLS_DIR, is_frozen())
    return cmd, rbm.find_deno(TOOLS_DIR), rbm.find_ffmpeg(_SCRIPT_DIR, TOOLS_DIR)


# ── 안내 메시지 ───────────────────────────────────────────────────────────
def _np_embed(p: GuildPlayer) -> discord.Embed:
    t = p.current
    vc = p.voice()
    paused = bool(vc and vc.is_paused())
    e = discord.Embed(
        title=("⏸ 일시정지" if paused else "🎵 재생 중"),
        description=f"**[{t.title}]({t.url})**",
        color=0x95A5A6 if paused else 0xE74C3C,
    )
    e.add_field(name="길이", value=t.duration_text)
    e.add_field(name="신청", value=t.requester_name or "-")
    e.add_field(name="볼륨", value=f"{round(p.volume * 100)}%")
    if vc and vc.channel:
        e.add_field(name="채널", value=f"{vc.channel.mention} · {p.client.user.display_name}")
    if len(p.queue):
        nxt = p.queue.items[0]
        e.set_footer(text=f"다음 곡: {nxt.title[:60]}  (대기 {len(p.queue)}곡)")
    return e


def _queue_text(p: GuildPlayer, limit: int = 15) -> str:
    lines = []
    if p.current:
        lines.append(f"▶ **{p.current.title}** `{p.current.duration_text}` — {p.current.requester_name}")
    for i, t in enumerate(list(p.queue.items)[:limit], 1):
        lines.append(f"`{i}.` {t.title} `{t.duration_text}` — {t.requester_name}")
    if len(p.queue) > limit:
        lines.append(f"… 외 {len(p.queue) - limit}곡")
    return "\n".join(lines) if lines else "대기열이 비어 있습니다."


async def _delete_np(p: GuildPlayer):
    msg, p.np_message = p.np_message, None
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass


async def _post_np(p: GuildPlayer):
    await _delete_np(p)
    ch = bot.get_channel(p.text_channel_id)
    if ch is None or p.current is None:
        return
    try:
        p.np_message = await ch.send(embed=_np_embed(p), view=MusicControlView())
    except Exception as e:
        log.warning(f"[안내] 재생 메시지 전송 실패: {type(e).__name__}: {e}")


async def _refresh_np(p: GuildPlayer):
    if p.np_message and p.current:
        try:
            await p.np_message.edit(embed=_np_embed(p))
        except Exception:
            pass


# ── 재생 흐름 ─────────────────────────────────────────────────────────────
class YTDLPAudio(discord.FFmpegPCMAudio):
    """yt-dlp가 받은 오디오를 파이프로 ffmpeg에 넘겨 PCM으로 재생. 끝나거나 건너뛰면 yt-dlp도 종료."""

    def __init__(self, cmd: list, page_url: str, deno: str, ffmpeg: str):
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW
        self.last_error = ""
        self._ytdlp = subprocess.Popen(cmd + rbm.ytdlp_pipe_args(page_url, deno),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, **kwargs)
        threading.Thread(target=self._read_errors, daemon=True).start()
        try:
            # pipe=True면 ffmpeg가 표준입력(-i -)을 읽으므로 -nostdin을 주면 안 된다
            super().__init__(self._ytdlp.stdout, pipe=True, executable=ffmpeg or "ffmpeg",
                             options=rbm.FFMPEG_OPTIONS)
        except Exception:
            self._kill_ytdlp()
            raise

    def _read_errors(self):
        try:
            for raw in self._ytdlp.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    if line.startswith("ERROR"):
                        self.last_error = line[:300]
                    log.warning(f"[yt-dlp] {line[:300]}")
        except Exception:
            pass

    def _kill_ytdlp(self):
        proc, self._ytdlp = getattr(self, "_ytdlp", None), None
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            self._kill_ytdlp()


async def _report_failed(p: GuildPlayer, track, err: str):
    ch = bot.get_channel(p.text_channel_id)
    if ch:
        try:
            await ch.send(f"⚠ 재생할 수 없어 건너뜁니다: **{track.title}**\n`{err}`")
        except Exception:
            pass

def _cancel_idle(p: GuildPlayer):
    if p.idle_task and not p.idle_task.done():
        p.idle_task.cancel()
    p.idle_task = None
    p.idle_empty = False


def _schedule_idle(p: GuildPlayer, empty: bool = False):
    """empty=True: 채널이 비어서 거는 타이머(EMPTY_DISCONNECT). 이미 걸려 있으면 다시 걸지 않는다
    — 다른 사람의 음성 상태 변화마다 타이머가 처음부터 다시 시작되면 안 되므로."""
    if empty and p.idle_empty and p.idle_task and not p.idle_task.done():
        return
    _cancel_idle(p)
    delay = EMPTY_DISCONNECT if empty else IDLE_DISCONNECT

    async def _idle():
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        vc = p.voice()
        if vc is None or vc.channel is None:
            return
        alone = not any(not m.bot for m in vc.channel.members)
        if alone or (not vc.is_playing() and not vc.is_paused()):
            log.info(f"[자동 퇴장] {vc.channel} — {delay}초 동안 {'듣는 사람' if alone else '재생'} 없음")
            await _shutdown(p)
    p.idle_task = asyncio.create_task(_idle())
    p.idle_empty = empty


async def _shutdown(p: GuildPlayer):
    _cancel_idle(p)
    p.queue.clear()
    p.current = None
    vc = p.voice()
    if vc:
        try:
            vc.stop()
            await vc.disconnect(force=False)
        except Exception:
            pass
    await _delete_np(p)


def _audio_state(vc) -> dict:
    """음성 전송 상태 (보낸 프레임 수, E2EE(DAVE) 준비 여부)."""
    conn = getattr(vc, "_connection", None)
    player = getattr(vc, "_player", None)
    return {
        "frames": getattr(player, "loops", 0),
        "dave": getattr(conn, "dave_protocol_version", 0),
        "encrypt": bool(getattr(conn, "can_encrypt", False)),
    }


async def _report_audio_state(p: GuildPlayer, vc, track):
    """재생 5초 뒤 실제로 소리를 보내고 있는지 로그에 남긴다. 디스코드 E2EE 통화인데 암호화가 준비되지 않았으면
    듣는 사람에게는 말하는 불만 들어오고 소리가 안 들리므로, E2EE 세션을 한 번 새로 맺는다."""
    await asyncio.sleep(5)
    if p.voice() is not vc or p.current is not track or not vc.is_playing():
        return
    st = _audio_state(vc)
    log.info(f"[전송 상태] {p.client.user} @ {vc.channel} | 프레임 {st['frames']} | "
             f"DAVE v{st['dave']} 암호화 {'준비됨' if st['encrypt'] else '안 됨'}")
    if st["frames"] < 50:
        log.warning(f"[전송 상태] {p.client.user}: 5초 동안 보낸 소리가 거의 없음 — yt-dlp/ffmpeg 입력이 막혔을 수 있음")
    if st["dave"] and not st["encrypt"]:
        log.warning(f"[전송 상태] {p.client.user}: E2EE(DAVE) 세션이 준비되지 않아 소리가 안 들릴 수 있음 — 세션 재설정")
        try:
            await vc._connection.reinit_dave_session()
        except Exception as e:
            log.warning(f"[전송 상태] DAVE 재설정 실패: {type(e).__name__}: {e}")


async def play_next(p: GuildPlayer):
    """대기열 다음 곡 재생. 재생할 곡이 없으면 자동 퇴장 타이머를 건다."""
    async with p.lock:
        vc = p.voice()
        if vc is None or not vc.is_connected():
            p.current = None
            return
        if vc.is_playing() or vc.is_paused():
            return
        cmd, deno, ffmpeg = _tools()
        while True:
            track = p.queue.pop()
            if track is None:
                p.current = None
                await _delete_np(p)
                _schedule_idle(p)
                return
            try:
                src = YTDLPAudio(cmd, track.url, deno, ffmpeg)
                source = discord.PCMVolumeTransformer(src, volume=p.volume)
            except Exception as e:
                log.warning(f"[재생 실패] {track.title}: {type(e).__name__}: {e}")
                await _report_failed(p, track, str(e))
                continue
            break

        _cancel_idle(p)
        p.current = track
        p.started_at = time.time()

        started = time.time()

        def _after(err):
            if err:
                log.warning(f"[재생 오류] {track.title}: {err}")
            # 바로 끝났으면 yt-dlp가 못 받은 것 → 채널에 이유 안내
            if time.time() - started < 5 and (src.last_error or err):
                asyncio.run_coroutine_threadsafe(_report_failed(p, track, src.last_error or str(err)), bot.loop)
            asyncio.run_coroutine_threadsafe(play_next(p), bot.loop)

        # 음악용 Opus 설정: 비트레이트를 채널에 맞춰 올리고, 손실 보정(FEC)에 쓰던 비트를 음질에 쓴다
        kbps = rbm.voice_bitrate_kbps(getattr(vc.channel, "bitrate", 0), BITRATE_KBPS)
        vc.play(source, after=_after, bitrate=kbps, signal_type="music", fec=False, expected_packet_loss=0.01)
        log.info(f"[재생] {track.title} ({track.duration_text}) — {track.requester_name} | {kbps}kbps"
                 f" | {p.client.user} @ {vc.channel}({vc.channel.id})")
        asyncio.create_task(_report_audio_state(p, vc, track))
    await _post_np(p)


# ── 권한/검사 ─────────────────────────────────────────────────────────────
def _is_manager(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    return any(r.id in CONTROL_ROLE_IDS for r in getattr(member, "roles", []))


def _can_control(member: discord.Member, p: GuildPlayer) -> bool:
    vc = p.voice()
    if _is_manager(member):
        return True
    return bool(vc and member.voice and member.voice.channel and member.voice.channel.id == vc.channel.id)


async def _check_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return False
    if not COMMAND_CHANNEL_IDS or interaction.channel_id in COMMAND_CHANNEL_IDS:
        return True
    if ALLOW_VOICE_CHAT and isinstance(interaction.channel, (discord.VoiceChannel, discord.StageChannel)):
        return True
    mentions = " ".join(f"<#{c}>" for c in COMMAND_CHANNEL_IDS)
    where = f"{mentions} 또는 음성채널 채팅" if ALLOW_VOICE_CHAT else mentions
    await interaction.response.send_message(f"음악 명령은 {where}에서 사용해 주세요.", ephemeral=True)
    return False


async def _no_player(interaction: discord.Interaction):
    many = sum(1 for p in _guild_players(interaction.guild.id) if p.connected())
    if many > 1:
        msg = (f"지금 {many}개 채널에서 재생 중이에요. 조작할 음악이 나오는 음성채널에 들어가거나, "
               "그 채널의 재생 패널 버튼을 눌러 주세요.")
    else:
        msg = "지금 재생 중인 음악이 없습니다."
    await interaction.response.send_message(msg, ephemeral=True)


async def _check_control(interaction: discord.Interaction) -> GuildPlayer | None:
    if not await _check_channel(interaction):
        return None
    p = _find_player(interaction)
    if p is None or not p.connected():
        await _no_player(interaction)
        return None
    if not _can_control(interaction.user, p):
        await interaction.response.send_message("봇과 같은 음성채널에 있어야 조작할 수 있습니다.", ephemeral=True)
        return None
    return p


# ── 동작 (명령/버튼 공용) ─────────────────────────────────────────────────
async def do_skip(interaction: discord.Interaction):
    p = await _check_control(interaction)
    if not p:
        return
    vc = p.voice()
    if not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("건너뛸 곡이 없습니다.", ephemeral=True)
        return
    title = p.current.title if p.current else "현재 곡"
    vc.stop()   # after 콜백이 다음 곡을 재생
    await interaction.response.send_message(f"⏭ **{title}** 건너뜀 — {interaction.user.display_name}")


async def do_pause(interaction: discord.Interaction):
    p = await _check_control(interaction)
    if not p:
        return
    vc = p.voice()
    if vc.is_paused():
        vc.resume()
        msg = "▶ 다시 재생합니다."
    elif vc.is_playing():
        vc.pause()
        msg = "⏸ 일시정지했습니다."
    else:
        await interaction.response.send_message("재생 중인 곡이 없습니다.", ephemeral=True)
        return
    await interaction.response.send_message(msg, ephemeral=True)
    await _refresh_np(p)


async def do_stop(interaction: discord.Interaction):
    p = await _check_control(interaction)
    if not p:
        return
    await interaction.response.send_message(f"⏹ 재생을 멈추고 나갑니다 — {interaction.user.display_name}")
    await _shutdown(p)


async def do_queue(interaction: discord.Interaction):
    if not await _check_channel(interaction):
        return
    p = _find_player(interaction)
    if p is None:
        await _no_player(interaction)
        return
    e = discord.Embed(title=f"📜 대기열 ({len(p.queue)}곡)", description=_queue_text(p), color=0x3498DB)
    await interaction.response.send_message(embed=e, ephemeral=True)


VOLUME_STEP = 10   # 재생 패널 🔉/🔊 버튼 한 번에 바뀌는 볼륨(%)


def _apply_volume(p: GuildPlayer, percent: int) -> int:
    """볼륨을 1~100%로 맞춰 재생 중인 곡에도 바로 적용. 적용된 값(%)을 돌려준다."""
    percent = max(1, min(100, int(percent)))
    p.volume = percent / 100
    vc = p.voice()
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    return percent


async def do_volume_step(interaction: discord.Interaction, delta: int):
    p = await _check_control(interaction)
    if not p:
        return
    before = round(p.volume * 100)
    now = _apply_volume(p, before + delta)
    if now == before:
        edge = "최대" if delta > 0 else "최소"
        await interaction.response.send_message(f"🔊 이미 {edge} 볼륨이에요 ({now}%).", ephemeral=True)
        return
    # 패널 메시지의 볼륨 칸을 바로 갱신 (버튼 응답으로 메시지 수정)
    if p.current and p.np_message and interaction.message and interaction.message.id == p.np_message.id:
        await interaction.response.edit_message(embed=_np_embed(p))
    else:
        await interaction.response.send_message(f"🔊 볼륨 {now}%", ephemeral=True)
        await _refresh_np(p)


class MusicControlView(discord.ui.View):
    """재생 메시지 버튼 — custom_id 고정 + timeout 없음 (봇 재시작 후에도 눌리면 현재 상태 기준으로 동작)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="rb_music:pause")
    async def _pause(self, interaction: discord.Interaction, _b):
        await do_pause(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.primary, custom_id="rb_music:skip")
    async def _skip(self, interaction: discord.Interaction, _b):
        await do_skip(interaction)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="rb_music:stop")
    async def _stop(self, interaction: discord.Interaction, _b):
        await do_stop(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="rb_music:queue")
    async def _queue(self, interaction: discord.Interaction, _b):
        await do_queue(interaction)

    @discord.ui.button(emoji="🔉", label=f"-{VOLUME_STEP}", style=discord.ButtonStyle.secondary,
                       custom_id="rb_music:voldown", row=1)
    async def _vol_down(self, interaction: discord.Interaction, _b):
        await do_volume_step(interaction, -VOLUME_STEP)

    @discord.ui.button(emoji="🔊", label=f"+{VOLUME_STEP}", style=discord.ButtonStyle.secondary,
                       custom_id="rb_music:volup", row=1)
    async def _vol_up(self, interaction: discord.Interaction, _b):
        await do_volume_step(interaction, VOLUME_STEP)


async def _connect(p: GuildPlayer, channel_id: int):
    """그 봇으로 음성채널에 들어간다 (연결은 그 봇의 채널 객체로 해야 그 봇이 들어간다)."""
    channel = p.client.get_channel(channel_id)
    vc = p.voice()
    if vc is not None and vc.is_connected():
        if vc.channel.id != channel_id:
            await vc.move_to(channel)
        return vc
    if vc is not None:   # 이전 연결 시도가 덜 끝난 채 남아 있으면 정리
        await _drop_voice(p)
    return await channel.connect(self_deaf=True, timeout=15, reconnect=False)


async def _drop_voice(p: GuildPlayer):
    vc = p.voice()
    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass


def _voice_warning(vc) -> str:
    """봇이 들어갔지만 소리를 낼 수 없는 상태(말하기 권한 없음·서버 음소거)를 안내 문구로 돌려준다."""
    try:
        me = vc.channel.guild.me
        if not vc.channel.permissions_for(me).speak:
            return "말하기 권한이 없어 소리가 나지 않아요. 채널 권한에서 이 봇에 '말하기'를 허용해 주세요."
        if me.voice and (me.voice.mute or me.voice.suppress):
            return "서버 음소거 상태라 소리가 나지 않아요. 봇의 서버 음소거를 풀어 주세요."
    except Exception:
        pass
    return ""


def _all_busy_text(guild_id: int, channel_id: int = 0) -> str:
    total = len(_clients())
    joined = [c for c in _clients() if c.get_guild(guild_id) is not None]
    no_perm = []
    if channel_id:
        for c in joined:
            miss = _missing_voice_perms(c, channel_id)
            if miss:
                no_perm.append(f"{c.user.display_name}({'·'.join(miss)})")
    in_use = sum(1 for c in joined if get_player(c, guild_id).connected())
    text = (f"🎵 현재 사용 가능한 뮤직봇이 없어요 ({in_use}/{len(joined)}). "
            "다른 채널 재생이 끝나면 다시 시도해 주세요.")
    if no_perm:
        text += f"\n이 채널 권한이 없는 봇: {', '.join(no_perm)} — 채널 권한에서 허용해 주세요."
    if len(joined) < total:
        text += f"\n(이 서버에 초대되지 않은 음악봇이 {total - len(joined)}개 있어요)"
    return text


# ── 슬래시 명령 ───────────────────────────────────────────────────────────
@bot.tree.command(name="재생", description="유튜브 링크(영상·재생목록) 또는 검색어로 음악을 재생합니다")
@app_commands.describe(링크="유튜브 링크 또는 검색어")
async def cmd_play(interaction: discord.Interaction, 링크: str):
    if not await _check_channel(interaction):
        return
    member = interaction.user
    if not (member.voice and member.voice.channel):
        await interaction.response.send_message("먼저 음성채널에 들어가 주세요.", ephemeral=True)
        return
    query = 링크.strip().strip("<>")
    if rbm.is_url(query) and YOUTUBE_ONLY and not rbm.is_youtube_url(query):
        await interaction.response.send_message("유튜브 링크만 재생할 수 있습니다.", ephemeral=True)
        return
    cmd, deno, ffmpeg = _tools()
    if not cmd or not ffmpeg:
        missing = ", ".join(n for n, ok in (("yt-dlp", cmd), ("FFmpeg", ffmpeg)) if not ok)
        await interaction.response.send_message(f"⚠ 재생 도구가 준비되지 않았습니다 ({missing}). 잠시 후 다시 시도해 주세요.",
                                                ephemeral=True)
        return

    gid = interaction.guild.id
    target = member.voice.channel
    if _pick_player(gid, target.id) is None:
        await interaction.response.send_message(_all_busy_text(gid, target.id), ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        tracks = await rbm.lookup(cmd, query, deno, member.id, member.display_name, limit=MAX_PLAYLIST)
    except Exception as e:
        log.warning(f"[검색 실패] {query}: {e}")
        await interaction.followup.send(f"⚠ 영상을 불러오지 못했습니다.\n`{e}`")
        return
    if not tracks:
        await interaction.followup.send("⚠ 재생할 영상을 찾지 못했습니다.")
        return

    # 봇 고르기 → 음성 연결 → 대기열 추가를 한 번에 (그 사이 다른 /재생이 같은 봇을 가져가지 않게)
    lock = _assign_locks.setdefault(gid, asyncio.Lock())
    async with lock:
        failed = []
        while True:
            p = _pick_player(gid, target.id, exclude=failed)
            if p is None:
                if failed:
                    names = ", ".join(c.user.display_name for c in failed)
                    await interaction.followup.send(
                        f"⚠ 음성채널에 들어가지 못했습니다 ({names}). 그 봇에 이 채널 연결·말하기 권한이 있는지 확인해 주세요.")
                else:
                    await interaction.followup.send(_all_busy_text(gid, target.id))
                return
            try:
                vc = await _connect(p, target.id)
                break
            except Exception as e:
                log.warning(f"[음성 연결 실패] {target} ({p.client.user}): {type(e).__name__}: {e} — 다른 봇으로 다시 시도")
                failed.append(p.client)
                await _drop_voice(p)
        warn = _voice_warning(vc)
        busy = vc.is_playing() or vc.is_paused() or p.current is not None
        if not busy or not p.text_channel_id:   # 재생 중이면 안내 메시지는 처음 /재생한 채널에 계속
            p.text_channel_id = interaction.channel_id
        added = p.queue.add(tracks)
    if warn:
        log.warning(f"[음성 권한] {p.client.user} @ {vc.channel}: {warn}")
        await interaction.followup.send(f"⚠ **{p.client.user.display_name}** 봇이 {vc.channel.mention}에서 {warn}")
    if added == 0:
        await interaction.followup.send(f"⚠ 대기열이 가득 찼어요 (최대 {MAX_QUEUE}곡). 곡이 끝나거나 `/삭제`로 빼면 다시 추가할 수 있어요.")
        return
    if added == 1:
        t = tracks[0]
        head = "🎵 대기열에 추가" if busy else "🎵 곧 재생합니다"
        await interaction.followup.send(f"{head}: **{t.title}** `{t.duration_text}`")
    else:
        extra = f" (대기열이 가득 차 {len(tracks) - added}곡은 제외)" if added < len(tracks) else ""
        await interaction.followup.send(f"🎵 재생목록에서 **{added}곡**을 추가했습니다{extra}.")
    if not busy:
        await play_next(p)


@bot.tree.command(name="건너뛰기", description="지금 곡을 건너뛰고 다음 곡을 재생합니다")
async def cmd_skip(interaction: discord.Interaction):
    await do_skip(interaction)


@bot.tree.command(name="일시정지", description="일시정지 / 다시 재생")
async def cmd_pause(interaction: discord.Interaction):
    await do_pause(interaction)


@bot.tree.command(name="정지", description="재생을 멈추고 대기열을 비운 뒤 음성채널에서 나갑니다")
async def cmd_stop(interaction: discord.Interaction):
    await do_stop(interaction)


@bot.tree.command(name="대기열", description="재생 대기열을 봅니다")
async def cmd_queue(interaction: discord.Interaction):
    await do_queue(interaction)


@bot.tree.command(name="현재곡", description="지금 재생 중인 곡을 봅니다")
async def cmd_now(interaction: discord.Interaction):
    if not await _check_channel(interaction):
        return
    p = _find_player(interaction)
    if p is None or not p.current:
        await interaction.response.send_message("지금 재생 중인 곡이 없습니다.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_np_embed(p), ephemeral=True)


@bot.tree.command(name="삭제", description="대기열에서 곡을 뺍니다 (번호는 /대기열 참고)")
@app_commands.describe(번호="대기열 번호")
async def cmd_remove(interaction: discord.Interaction, 번호: app_commands.Range[int, 1, 500]):
    p = await _check_control(interaction)
    if not p:
        return
    t = p.queue.remove(번호)
    if t is None:
        await interaction.response.send_message(f"{번호}번 곡이 없습니다.", ephemeral=True)
        return
    await interaction.response.send_message(f"🗑 대기열에서 뺐습니다: **{t.title}**")
    await _refresh_np(p)


@bot.tree.command(name="볼륨", description="볼륨을 바꿉니다 (1~100)")
@app_commands.describe(값="1~100")
async def cmd_volume(interaction: discord.Interaction, 값: app_commands.Range[int, 1, 100]):
    p = await _check_control(interaction)
    if not p:
        return
    _apply_volume(p, 값)
    await interaction.response.send_message(f"🔊 볼륨 {값}%", ephemeral=True)
    await _refresh_np(p)


# ── 이벤트 ────────────────────────────────────────────────────────────────
async def _register_guild(guild: discord.Guild, only_missing: bool = False) -> int:
    """서버에 슬래시 명령 등록 (전역 동기화는 반영까지 최대 1시간이라 서버별로).
    전용 토큰이면 전체 동기화, 보스봇과 같은 토큰이면 음악 명령만 하나씩 upsert (보스봇 명령 유지)."""
    if not SHARED_TOKEN:
        bot.tree.copy_global_to(guild=guild)
        return len(await bot.tree.sync(guild=guild))
    # 이미 같은 내용으로 등록된 명령은 건너뛴다 (매번 8개를 다시 올리면 429 속도 제한에 걸림)
    existing = {c.name: c for c in await bot.tree.fetch_commands(guild=guild)}
    n = 0
    for cmd in bot.tree.get_commands():
        payload = cmd.to_dict(bot.tree)
        old = existing.get(cmd.name)
        if old is not None and (only_missing or _same_command(old, payload)):
            continue
        await bot.http.upsert_guild_command(bot.application_id, guild.id, payload)
        n += 1
    return n


def _same_command(old, payload: dict) -> bool:
    def _opts(opts):
        return [(o.get("name"), o.get("type"), o.get("description"), bool(o.get("required"))) for o in opts or []]
    return (old.description == payload.get("description")
            and _opts(rbm.clean_command_payload(old.to_dict()).get("options")) == _opts(payload.get("options")))


async def _sync_slash_commands():
    for guild in bot.guilds:
        try:
            n = await _register_guild(guild)
            log.info(f"[슬래시 등록] {guild.name}({guild.id}) {n}개{' (보스봇과 토큰 공유)' if SHARED_TOKEN else ''}")
        except Exception as e:
            log.warning(f"[슬래시 등록 실패] {guild.name}({guild.id}): {type(e).__name__}: {e}")


async def _command_guard_loop():
    """토큰 공유 시 보스봇이 나중에 전체 동기화하면 음악 명령이 빠질 수 있어 주기적으로 확인해 되살린다."""
    for delay in [60, 180] + [600] * 10**6:
        await asyncio.sleep(delay)
        for guild in bot.guilds:
            try:
                n = await _register_guild(guild, only_missing=True)
                if n:
                    log.info(f"[슬래시 복구] {guild.name} — 빠진 음악 명령 {n}개 다시 등록")
            except Exception as e:
                log.warning(f"[슬래시 복구 실패] {guild.name}: {type(e).__name__}: {e}")


def _check_voice_libs():
    """음성 재생에 필요한 라이브러리 점검 — 빠졌으면 원인을 로그에 남긴다."""
    for mod in ("nacl.secret", "davey"):
        try:
            __import__(mod)
        except Exception as e:
            log.warning(f"[음성 라이브러리] {mod} 불러오기 실패 — 음성 재생 불가: {type(e).__name__}: {e}")
    try:
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        log.info(f"[음성 라이브러리] opus={'OK' if discord.opus.is_loaded() else '없음'}")
    except Exception as e:
        log.warning(f"[음성 라이브러리] opus 불러오기 실패: {type(e).__name__}: {e}")


async def _prepare_tools():
    try:
        got = await asyncio.to_thread(rbm.install_tools, TOOLS_DIR, log)
        if got:
            log.info(f"[도구] 설치 완료: {', '.join(got)}")
    except Exception as e:
        log.warning(f"[도구] 다운로드 실패: {type(e).__name__}: {e}")
    cmd, deno, ffmpeg = _tools()
    log.info(f"[도구] yt-dlp={' '.join(cmd) or '없음'} | ffmpeg={ffmpeg or '없음'} | deno={deno or '없음'}")


async def _update_loop():
    """yt-dlp 단독 실행 파일을 하루 한 번 최신으로 (유튜브 변경 대응)."""
    while True:
        cmd, _, _ = _tools()
        if cmd:
            try:
                res = await rbm.self_update(cmd)
                if res:
                    log.info(f"[yt-dlp 업데이트] {res}")
            except Exception as e:
                log.warning(f"[yt-dlp 업데이트 실패] {type(e).__name__}: {e}")
        await asyncio.sleep(24 * 3600)


_ready_once = False


# ── ⚔️ 전투지원 음성 안내 (보스봇 IPC: {"cmd":"announce","args":[문장, 목소리, [대화방 ID…]]}) ──────────
IPC_PORT = int(config.get("ipc_port", 7705) or 7705)
ANNOUNCE_TMP = os.path.join(_RUNTIME_DIR, "Tools", "announce")
ANNOUNCE_LEAD_SEC = max(0.0, float(config.get("announce_lead_seconds", 1.0) or 0))   # 입장 후 읽기 전 대기


async def _announce_clip(text: str, voice: str) -> str:
    import edge_tts
    import RaidBook_tts as rbt
    os.makedirs(ANNOUNCE_TMP, exist_ok=True)
    path = os.path.join(ANNOUNCE_TMP, f"announce_{time.monotonic_ns()}.mp3")
    await edge_tts.Communicate(text, rbt.VOICES[rbt.voice_key(voice)]).save(path)
    return path


async def _stop_music_for_announce(p: GuildPlayer):
    """음악 재생 중인 봇이면 재생 종료 (대기열·재생 패널 정리, 음성채널에는 남는다)."""
    vc = p.voice()
    if not p.busy():
        return
    log.info(f"[전투지원] {p.client.user}: 음악 재생 종료 후 안내 — {p.current.title if p.current else ''}")
    p.queue.clear()
    p.current = None
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await _delete_np(p)


async def _wait_voice_ready(p: GuildPlayer, vc, timeout: float = 6.0):
    """막 들어간 음성채널은 디스코드 E2EE(DAVE) 키 교환이 끝나기 전에 보낸 소리가 듣는 사람에게 안 들린다
    (2026-11-05: 새로 들어간 봇이 읽은 대화방 2곳에서 안내가 안 들림). 암호화가 준비될 때까지 기다리고,
    안 되면 세션을 한 번 새로 맺는다. 준비된 뒤에도 첫 소리가 잘리지 않게 잠깐 쉰다."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    reinit = False
    while loop.time() < end:
        st = _audio_state(vc)
        if vc.is_connected() and (not st["dave"] or st["encrypt"]):
            break
        if not reinit and loop.time() > end - timeout / 2 and st["dave"] and not st["encrypt"]:
            reinit = True
            log.warning(f"[전투지원] {p.client.user} @ {vc.channel}: E2EE 준비 안 됨 — 세션 재설정")
            try:
                await vc._connection.reinit_dave_session()
            except Exception as e:
                log.warning(f"[전투지원] DAVE 재설정 실패: {type(e).__name__}: {e}")
        await asyncio.sleep(0.1)
    await asyncio.sleep(ANNOUNCE_LEAD_SEC)


async def _announce_with(p: GuildPlayer, channel_ids: list, clip: str):
    for cid in channel_ids:
        try:
            await _stop_music_for_announce(p)
            async with p.lock:
                vc = await _connect(p, cid)
                await _wait_voice_ready(p, vc)
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                done = asyncio.Event()
                loop = asyncio.get_running_loop()
                vc.play(discord.FFmpegPCMAudio(clip, executable=_tools()[2] or "ffmpeg", before_options="-nostdin"),
                        after=lambda err: loop.call_soon_threadsafe(done.set))
                try:
                    await asyncio.wait_for(done.wait(), timeout=30)
                except asyncio.TimeoutError:
                    vc.stop()
            st = _audio_state(vc)
            log.info(f"[전투지원] {p.client.user} @ {vc.channel} 안내 완료 | 프레임 {st['frames']} | "
                     f"DAVE v{st['dave']} 암호화 {'준비됨' if st['encrypt'] else '안 됨'}")
            _schedule_idle(p)   # 안내 뒤 할 일 없으면 idle_disconnect_seconds 뒤 퇴장
        except Exception as e:
            log.warning(f"[전투지원] {p.client.user} → 채널 {cid} 안내 실패: {type(e).__name__}: {e}")
            await _drop_voice(p)


async def _announce(text: str, voice, channel_ids: list) -> dict:
    ids = [int(c) for c in channel_ids or [] if str(c).strip().isdigit()]
    chans = [bot.get_channel(c) for c in ids]
    guild = next((c.guild for c in chans if c is not None), None)
    if guild is None:
        return {"ok": False, "error": "대화방 채널을 찾을 수 없음"}
    async with _assign_locks.setdefault(guild.id, asyncio.Lock()):
        cands = []
        for c in _clients():
            if c.get_guild(guild.id) is None:
                continue
            p = get_player(c, guild.id)
            cands.append((p, p.voice().channel.id if p.connected() else None, p.busy()))
        plan = rbm.assign_announce(cands, ids, lambda p, cid: not _missing_voice_perms(p.client, cid))
    if not plan:
        return {"ok": False, "error": "사용할 수 있는 음악봇이 없음 (채널 권한 확인)"}
    try:
        clip = await _announce_clip(text, voice)
    except Exception as e:
        return {"ok": False, "error": f"음성 생성 실패: {type(e).__name__}: {e}"}

    async def _run():
        try:
            await asyncio.gather(*(_announce_with(p, cs, clip) for p, cs in plan))
        finally:
            try:
                os.remove(clip)
            except OSError:
                pass
    asyncio.create_task(_run())
    assigned = sum(len(cs) for _, cs in plan)
    log.info(f"[전투지원] 안내 '{text}' — 대화방 {assigned}/{len(ids)}곳, 음악봇 {len(plan)}개 "
             f"({', '.join(f'{p.client.user}→{cs}' for p, cs in plan)})")
    return {"ok": True, "channels": assigned, "bots": len(plan)}


async def _ipc_handle(reader, writer):
    try:
        req = json.loads((await asyncio.wait_for(reader.readline(), timeout=10)).decode("utf-8"))
        args = req.get("args") or []
        if req.get("cmd") == "announce" and len(args) >= 3 and args[0]:
            resp = await _announce(str(args[0]), args[1], list(args[2] or []))
        else:
            resp = {"ok": False, "error": f"알 수 없는 명령: {req.get('cmd')}"}
    except Exception as e:
        resp = {"ok": False, "error": f"IPC 오류: {e}"}
    try:
        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
    except Exception:
        pass
    writer.close()


_ipc_server = None


async def _start_ipc():
    global _ipc_server
    if _ipc_server is not None:
        return
    try:
        _ipc_server = await asyncio.start_server(_ipc_handle, "127.0.0.1", IPC_PORT)
        log.info(f"[IPC] 전투지원 음성 안내 서버 127.0.0.1:{IPC_PORT}")
    except Exception as e:
        log.warning(f"[IPC] 서버 시작 실패 (포트 {IPC_PORT}): {e}")


@bot.event
async def setup_hook():
    bot.add_view(MusicControlView())
    await _start_ipc()
    _check_voice_libs()
    await _prepare_tools()
    if AUTO_UPDATE:
        asyncio.create_task(_update_loop())


@bot.event
async def on_ready():
    global _ready_once
    log.info(f"[준비 완료] {bot.user} — 서버 {len(bot.guilds)}개")
    if _ready_once:
        return
    _ready_once = True
    await _sync_slash_commands()
    if SHARED_TOKEN:
        asyncio.create_task(_command_guard_loop())


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await _register_guild(guild)
    except Exception as e:
        log.warning(f"[슬래시 동기화 실패] {guild.name}: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    await _on_voice_state(bot, member, before, after)


async def _on_voice_state(client: discord.Client, member: discord.Member, before, after):
    """봇마다 자기 음성 연결만 본다 (member.guild는 그 봇의 캐시라 voice_client도 그 봇 것)."""
    vc = member.guild.voice_client
    p = players.get((id(client), member.guild.id))
    if p is None:
        return
    if member.id == client.user.id:
        if after.channel is None:   # 강제 퇴장/연결 끊김 → 상태 정리
            _cancel_idle(p)
            p.queue.clear()
            p.current = None
            await _delete_np(p)
        return
    if vc is None or vc.channel is None:
        return
    humans = [m for m in vc.channel.members if not m.bot]
    if not humans:
        if vc.is_playing():
            vc.pause()
            await _refresh_np(p)
        _schedule_idle(p, empty=True)   # 아무도 없으면 EMPTY_DISCONNECT(기본 1분) 뒤 재생 종료·퇴장
    elif before.channel != vc.channel and after.channel == vc.channel:
        _cancel_idle(p)
        if vc.is_paused() and p.current:
            vc.resume()
            await _refresh_np(p)
        elif not vc.is_playing() and not vc.is_paused():
            _schedule_idle(p)


def _read_token(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f).get("token") or "").strip()
    except Exception:
        return ""


def _extra_tokens(main_token: str) -> list:
    """동시 재생용 추가 봇 토큰. 빈 칸·중복·다른 봇(보스/Watchdog/TTS)과 같은 토큰은 뺀다 (토큰 값은 로그에 남기지 않음)."""
    others = {main_token}
    for name in ("boss_config.json", "watchdog_config.json", "tts_config.json"):
        t = _read_token(get_config_path(name))
        if t:
            others.add(t)
    out = []
    for i, raw in enumerate(config.get("extra_tokens") or [], 1):
        t = str(raw or "").strip()
        if not t:
            continue
        if t in others or t in out:
            log.warning(f"[추가 봇 {i}] 다른 봇과 같은 토큰이라 건너뜁니다 — 음악 전용 봇의 토큰을 넣어 주세요.")
            continue
        out.append(t)
    return out


def _make_worker(label: str) -> discord.Client:
    """음성 전용 추가 봇. 슬래시 명령·버튼은 메인 봇이 받으므로 음성 상태만 본다."""
    w = discord.Client(intents=intents)
    w.rb_label = label

    @w.event
    async def on_ready():
        log.info(f"[{label} 준비 완료] {w.user} — 서버 {len(w.guilds)}개")

    @w.event
    async def on_voice_state_update(member, before, after):
        await _on_voice_state(w, member, before, after)

    return w


async def _run_worker(w: discord.Client, token: str):
    try:
        await w.start(token)
    except discord.LoginFailure:
        log.warning(f"[{w.rb_label}] 토큰이 올바르지 않아 로그인하지 못했습니다 — music_config.json의 extra_tokens를 확인해 주세요.")
    except Exception as e:
        log.warning(f"[{w.rb_label}] 종료: {type(e).__name__}: {e}")


async def _main(token: str, extras: list):
    for i, t in enumerate(extras, 2):
        w = _make_worker(f"음악봇 {i}")
        workers.append(w)
        asyncio.create_task(_run_worker(w, t))
    async with bot:
        await bot.start(token)


def _wait_for_token() -> tuple:
    """(토큰, 보스봇과 공유 여부). 둘 다 비어 있으면 Watchdog 재시작 반복을 피하려고 종료하지 않고 기다린다."""
    warned = False
    boss_path = get_config_path("boss_config.json")
    while True:
        own = _read_token(CONFIG_PATH)
        boss = _read_token(boss_path)
        if own:
            return own, own == boss
        if boss:
            return boss, True
        if not warned:
            log.info("[대기] 토큰이 없습니다 — Boss Bot 토큰을 설정하면 자동으로 시작합니다.")
            warned = True
        time.sleep(30)


if __name__ == "__main__":
    _token, SHARED_TOKEN = _wait_for_token()
    _extras = _extra_tokens(_token)
    log.info(f"[시작] {'보스봇 토큰 공유' if SHARED_TOKEN else '전용 토큰'} | 추가 음악봇 {len(_extras)}개 "
             f"(동시 재생 최대 {len(_extras) or 1}개 채널{", 보스봇은 명령만" if _extras else ""})")
    asyncio.run(_main(_token, _extras))
