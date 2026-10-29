# -*- coding: utf-8 -*-
"""RaidBook Music Bot — 유튜브 링크를 음성채널에서 스트리밍 재생하는 별도 봇 (자체 토큰).

/재생 <유튜브 링크 또는 검색어> → 명령을 입력한 사람이 있는 음성채널에 들어가 재생.
/건너뛰기 /일시정지 /정지 /대기열 /삭제 /볼륨 /현재곡, 재생 메시지의 ⏯ ⏭ ⏹ 📜 버튼.
설정: Config/music_config.json — token이 비어 있으면 보스봇 토큰(boss_config.json)을 같이 쓴다.
같은 토큰이면 슬래시 명령을 덮어쓰지 않도록 자기 명령만 하나씩 등록한다 (RaidBook_music.MUSIC_COMMAND_NAMES).
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
# 같은 음성채널에 없어도 건너뛰기/정지 등을 할 수 있는 역할 (서버 관리 권한자는 항상 가능)
CONTROL_ROLE_IDS = _int_set(config.get("control_role_ids"))
DEFAULT_VOLUME = max(1, min(100, int(config.get("default_volume", 50) or 50)))
MAX_QUEUE = max(1, int(config.get("max_queue", 50) or 50))
MAX_PLAYLIST = max(1, int(config.get("max_playlist", 30) or 30))
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
    def __init__(self, guild_id: int):
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
        g = bot.get_guild(self.guild_id)
        return g.voice_client if g else None


players: dict = {}


def get_player(guild_id: int) -> GuildPlayer:
    p = players.get(guild_id)
    if p is None:
        p = players[guild_id] = GuildPlayer(guild_id)
    return p


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
        log.info(f"[재생] {track.title} ({track.duration_text}) — {track.requester_name} | {kbps}kbps")
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
    if COMMAND_CHANNEL_IDS and interaction.channel_id not in COMMAND_CHANNEL_IDS:
        mentions = " ".join(f"<#{c}>" for c in COMMAND_CHANNEL_IDS)
        await interaction.response.send_message(f"음악 명령은 {mentions} 에서 사용해 주세요.", ephemeral=True)
        return False
    return True


async def _check_control(interaction: discord.Interaction) -> GuildPlayer | None:
    if not await _check_channel(interaction):
        return None
    p = get_player(interaction.guild.id)
    vc = p.voice()
    if vc is None or not vc.is_connected():
        await interaction.response.send_message("지금 재생 중인 음악이 없습니다.", ephemeral=True)
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
    p = get_player(interaction.guild.id)
    e = discord.Embed(title=f"📜 대기열 ({len(p.queue)}곡)", description=_queue_text(p), color=0x3498DB)
    await interaction.response.send_message(embed=e, ephemeral=True)


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

    p = get_player(interaction.guild.id)
    target = member.voice.channel
    vc = p.voice()
    if vc and vc.is_connected() and vc.channel.id != target.id and (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message(f"지금 {vc.channel.mention} 에서 재생 중입니다. 그 채널로 와 주세요.",
                                                ephemeral=True)
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

    try:
        vc = p.voice()
        if vc is None or not vc.is_connected():
            vc = await target.connect(self_deaf=True, timeout=20)
        elif vc.channel.id != target.id:
            await vc.move_to(target)
    except Exception as e:
        log.warning(f"[음성 연결 실패] {target}: {type(e).__name__}: {e}")
        await interaction.followup.send(f"⚠ 음성채널에 들어가지 못했습니다. 봇에 연결·말하기 권한이 있는지 확인해 주세요.\n`{e}`")
        return

    p.text_channel_id = interaction.channel_id
    added = p.queue.add(tracks)
    if added == 0:
        await interaction.followup.send(f"⚠ 대기열이 가득 찼습니다 (최대 {MAX_QUEUE}곡).")
        return
    busy = vc.is_playing() or vc.is_paused()
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
    p = get_player(interaction.guild.id)
    if not p.current:
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
    p.volume = 값 / 100
    vc = p.voice()
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
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


@bot.event
async def setup_hook():
    bot.add_view(MusicControlView())
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
    vc = member.guild.voice_client
    p = players.get(member.guild.id)
    if p is None:
        return
    if member.id == bot.user.id:
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
    log.info(f"[시작] {'보스봇 토큰 공유' if SHARED_TOKEN else '전용 토큰'}")
    bot.run(_token, log_handler=None)
