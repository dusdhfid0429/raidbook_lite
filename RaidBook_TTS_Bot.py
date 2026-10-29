# -*- coding: utf-8 -*-
"""RaidBook TTS Bot — /tts 로 쓴 글을 음성채널에서 읽어 주는 별도 봇 (전용 토큰).

/tts 내용:<텍스트> [목소리:남성|여성] → 쓴 사람이 있는 음성채널에 들어가 읽고, 채널에 글과 🔄 다시 듣기 버튼을 남긴다.
/tts퇴장 /tts건너뛰기 /tts대기열.
설정: Config/tts_config.json — 보스봇·음악봇과 다른 전용 봇 토큰이 필요 (같은 봇이면 음성채널을 서로 뺏음).
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time

import discord
from discord import app_commands
from discord.ext import commands

import RaidBook_tts as rbt
from RaidBook_music import find_ffmpeg
from RaidBook_storage import DailyFileHandler
from config_helper import (
    ensure_config_from_template,
    ensure_runtime_tree,
    get_app_dir,
    get_config_path,
    get_runtime_dir,
)

# 설치판(PyInstaller)은 stdout이 CP949로 나가 Watchdog 로그 창 한글이 깨지므로 UTF-8로 고정 (보스봇·음악봇과 동일)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if "--selftest" in sys.argv:
    # 빌드 검증용 (build.ps1): 설치판 exe에 TTS·음성 라이브러리가 들어갔는지 확인하고 바로 종료
    _bad = []
    for _mod in ("edge_tts", "nacl.secret", "nacl.utils", "davey"):
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

import edge_tts  # noqa: E402  (selftest에서 먼저 확인)

_SCRIPT_DIR = get_app_dir()
_RUNTIME_DIR = get_runtime_dir()
ensure_runtime_tree()
CONFIG_PATH = get_config_path("tts_config.json")

if not ensure_config_from_template(CONFIG_PATH):
    print(f"❌ 설정 파일을 찾을 수 없습니다: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

LOG_DIR = os.path.join(_RUNTIME_DIR, "Log")
TOOLS_DIR = os.path.join(_RUNTIME_DIR, "Tools")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("TTSBot")
log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_console = logging.StreamHandler()
_console.setFormatter(_formatter)
log.addHandler(_console)
_file = DailyFileHandler(LOG_DIR, "RaidBook_TTS_Bot")
_file.setFormatter(_formatter)
log.addHandler(_file)


def _int_or_none(v):
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


MAX_QUEUE = max(1, int(config.get("max_queue", 10) or 10))
MAX_LENGTH = max(0, int(config.get("max_length", 0) or 0))   # 0=제한 없음 (전체판과 같음)
DEFAULT_VOICE = rbt.voice_key(config.get("default_voice", "2"))
READ_NAME = bool(config.get("read_name", True))
DEFAULT_VOICE_CHANNEL_ID = _int_or_none(config.get("default_voice_channel_id"))
IDLE_DISCONNECT = max(0, int(config.get("idle_disconnect_seconds", 0) or 0))   # 0=계속 머묾 (전체판과 같음)
IPC_PORT = int(config.get("ipc_port", 7704) or 7704)
REPLAY_ID = "rbtts:replay"   # persistent 버튼 — 재시작 후에도 메시지 내용으로 다시 읽음
TMP_DIR = os.path.join(tempfile.gettempdir(), "RaidBook_TTS")
os.makedirs(TMP_DIR, exist_ok=True)
FFMPEG = find_ffmpeg(_SCRIPT_DIR, TOOLS_DIR) or "ffmpeg"


def _allowed_ids() -> set:
    out = set()
    for v in config.get("allowed_channel_ids") or []:
        cid = _int_or_none(v)
        if cid:
            out.add(cid)
    return out


ALLOWED_CHANNEL_IDS = _allowed_ids()   # 비어 있으면 모든 채널

intents = discord.Intents.default()   # 슬래시 명령만 쓰므로 Message Content Intent 불필요
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)


# ── 서버별 읽기 대기열 ────────────────────────────────────────────────────
class GuildTTS:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.worker = None
        self.idle_task = None


_states: dict = {}


def _state(guild_id: int) -> GuildTTS:
    st = _states.get(guild_id)
    if st is None:
        st = _states[guild_id] = GuildTTS()
    return st


def _cancel_idle(st: GuildTTS):
    if st.idle_task and not st.idle_task.done():
        st.idle_task.cancel()
    st.idle_task = None


def _schedule_idle(guild: discord.Guild, st: GuildTTS):
    if not IDLE_DISCONNECT:
        return
    _cancel_idle(st)

    async def _leave_later():
        await asyncio.sleep(IDLE_DISCONNECT)
        vc = guild.voice_client
        if vc and not vc.is_playing() and st.queue.empty():
            await vc.disconnect(force=False)
            log.info(f"[퇴장] {IDLE_DISCONNECT}초 동안 읽을 메시지가 없어 음성채널에서 나감")

    st.idle_task = asyncio.create_task(_leave_later())


async def _enqueue(guild: discord.Guild, voice_channel, text: str, voice: str) -> bool:
    st = _state(guild.id)
    if st.queue.full():
        return False
    st.queue.put_nowait((voice_channel, text, voice))
    _cancel_idle(st)
    if st.worker is None or st.worker.done():
        st.worker = asyncio.create_task(_worker(guild, st))
    return True


async def _speak(guild: discord.Guild, voice_channel, text: str, voice: str):
    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    else:
        if vc:
            await vc.disconnect(force=True)
        vc = await voice_channel.connect(timeout=15, self_deaf=True)

    path = os.path.join(TMP_DIR, f"tts_{guild.id}_{time.monotonic_ns()}.mp3")
    try:
        await edge_tts.Communicate(text, rbt.VOICES[voice]).save(path)
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def _after(err):
            if err:
                log.warning(f"[재생 오류] {err}")
            loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(path, executable=FFMPEG, before_options="-nostdin"), after=_after)
        await done.wait()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _worker(guild: discord.Guild, st: GuildTTS):
    while not st.queue.empty():
        voice_channel, text, voice = st.queue.get_nowait()
        try:
            await _speak(guild, voice_channel, text, voice)
        except Exception as e:
            log.warning(f"[TTS 오류] {type(e).__name__}: {e} | 채널: {getattr(voice_channel, 'name', voice_channel)} | {text[:50]}")
            vc = guild.voice_client
            if vc and not vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        finally:
            st.queue.task_done()
    _schedule_idle(guild, st)


def _target_voice_channel(member):
    """작성자가 있는 음성채널, 없으면 config 기본 음성채널."""
    voice = getattr(member, "voice", None)
    if voice and voice.channel:
        return voice.channel
    if DEFAULT_VOICE_CHANNEL_ID:
        return bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
    return None


def _mention_names(guild: discord.Guild, text: str) -> dict:
    """슬래시 입력 속 <@id>/<@&id>/<#id> → 이름."""
    names = {}
    for kind, sid in rbt._MENTION.findall(text or ""):
        i = int(sid)
        obj = guild.get_role(i) if kind == "@&" else (guild.get_channel(i) if kind == "#" else guild.get_member(i))
        if obj is not None:
            names[i] = getattr(obj, "display_name", None) or obj.name
    return names


def _channel_allowed(channel_id: int) -> bool:
    return not ALLOWED_CHANNEL_IDS or channel_id in ALLOWED_CHANNEL_IDS


def _channel_hint() -> str:
    return " ".join(f"<#{c}>" for c in sorted(ALLOWED_CHANNEL_IDS))


async def _queue_speech(interaction: discord.Interaction, voice: str, name: str, text: str):
    """읽기 대기열에 넣는다. 문제가 있으면 안내 문구, 성공하면 None."""
    if not _channel_allowed(interaction.channel_id):
        return f"❌ TTS는 {_channel_hint()} 채널에서 써 주세요."
    voice_channel = _target_voice_channel(interaction.user)
    if voice_channel is None:
        return "❌ 음성채널에 먼저 입장해 주세요!"
    body = rbt.clean_for_speech(text, _mention_names(interaction.guild, text), MAX_LENGTH)
    if not body:
        return "❌ 읽을 내용이 없어요."
    if not await _enqueue(interaction.guild, voice_channel, rbt.speech_text(name, body, READ_NAME), voice):
        return f"❌ 대기열이 가득 찼어요! (최대 {MAX_QUEUE}개)"
    return None


class ReplayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="다시 듣기", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id=REPLAY_ID)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        parsed = rbt.parse_post(interaction.message.content if interaction.message else "")
        if not parsed:
            await interaction.response.send_message("❌ 다시 읽을 내용을 찾지 못했어요.", ephemeral=True)
            return
        voice, name, text = parsed
        err = await _queue_speech(interaction, voice, name, text)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.defer()
            log.info(f"[다시 듣기] {interaction.user.display_name} → {name}: {text[:60]}")


# ── 슬래시 명령 ───────────────────────────────────────────────────────────
@bot.tree.command(name="tts", description="입력한 글을 음성채널에서 읽어 줍니다")
@app_commands.describe(내용="읽어 줄 글 (앞에 1 = 남성, 2 = 여성)", 목소리="기본은 설정값(여성)")
@app_commands.choices(목소리=[app_commands.Choice(name="👩 여성", value="2"),
                           app_commands.Choice(name="👨 남성", value="1")])
@app_commands.guild_only()
async def cmd_tts(interaction: discord.Interaction, 내용: app_commands.Range[str, 1, 500],
                  목소리: app_commands.Choice[str] = None):
    # 전체판 #1/#2 처럼 '1 내용'(남성) / '2 내용'(여성)으로도 고를 수 있음 (옵션이 우선)
    prefixed, 내용 = rbt.split_voice_prefix(내용)
    if not 내용:
        await interaction.response.send_message("❌ 읽을 내용이 없어요.", ephemeral=True)
        return
    voice = rbt.voice_key(목소리.value if 목소리 else (prefixed or DEFAULT_VOICE))
    name = interaction.user.display_name
    err = await _queue_speech(interaction, voice, name, 내용)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    # 채팅에도 남겨서 못 들은 사람이 보거나 🔄로 다시 듣게 한다 (멘션 알림은 보내지 않음)
    await interaction.response.send_message(rbt.format_post(voice, name, 내용), view=ReplayView(),
                                            allowed_mentions=discord.AllowedMentions.none())
    log.info(f"[TTS] {name} | {rbt.VOICE_LABELS[voice]} | {내용[:60]} | 대기 {_state(interaction.guild.id).queue.qsize()}개")


@bot.tree.command(name="tts퇴장", description="TTS 봇을 음성채널에서 내보내고 대기열을 비웁니다")
@app_commands.guild_only()
async def cmd_leave(interaction: discord.Interaction):
    st = _state(interaction.guild.id)
    while not st.queue.empty():
        st.queue.get_nowait()
        st.queue.task_done()
    _cancel_idle(st)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
        await interaction.response.send_message("👋 음성채널에서 나갔어요.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 음성채널에 있지 않아요.", ephemeral=True)


@bot.tree.command(name="tts건너뛰기", description="지금 읽고 있는 TTS를 건너뜁니다")
@app_commands.guild_only()
async def cmd_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ 건너뛰었어요.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 읽고 있는 TTS가 없어요.", ephemeral=True)


@bot.tree.command(name="tts대기열", description="읽기 대기 중인 TTS 개수")
@app_commands.guild_only()
async def cmd_queue(interaction: discord.Interaction):
    n = _state(interaction.guild.id).queue.qsize()
    text = "📭 대기 중인 TTS가 없어요." if n == 0 else f"📬 대기 중인 TTS: **{n}개** / 최대 {MAX_QUEUE}개"
    await interaction.response.send_message(text, ephemeral=True)


# ── IPC (다른 봇이 음성 안내를 보낼 때: {"cmd":"speak","args":[텍스트, 목소리]}) ──
async def _ipc_speak(text: str, voice: str = None) -> dict:
    if not DEFAULT_VOICE_CHANNEL_ID:
        return {"ok": False, "error": "default_voice_channel_id 미설정"}
    ch = bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
    if ch is None:
        return {"ok": False, "error": "음성채널을 찾을 수 없음"}
    if not await _enqueue(ch.guild, ch, text, rbt.voice_key(voice or DEFAULT_VOICE)):
        return {"ok": False, "error": "대기열 가득 참"}
    log.info(f"[IPC speak] {text[:60]}")
    return {"ok": True}


async def _ipc_handle(reader, writer):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        req = json.loads(line.decode("utf-8"))
        args = req.get("args") or []
        if req.get("cmd") == "speak" and args and args[0]:
            resp = await _ipc_speak(str(args[0]), args[1] if len(args) > 1 else None)
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


# ── 시작 ──────────────────────────────────────────────────────────────────
async def _ensure_tts_channel():
    """읽을 채널이 비어 있으면 RaidBook 카테고리에 'tts' 채널을 만든다 (권한 없으면 모든 채널에서 동작)."""
    global ALLOWED_CHANNEL_IDS
    if not config.get("auto_create_channels", True):
        return
    from RaidBook_channel_setup import ChannelSpec, ensure_channels
    try:
        created, problems = await ensure_channels(
            bot, CONFIG_PATH, config, [ChannelSpec("allowed_channel_ids", "tts", is_list=True)], log)
    except Exception as e:
        log.warning(f"[채널 자동 생성 실패] {type(e).__name__}: {e}")
        return
    for p in problems:
        log.warning(f"[채널 자동 생성] {p} — 모든 채널에서 /tts를 받습니다")
    if created:
        ALLOWED_CHANNEL_IDS = _allowed_ids()


async def _sync_slash_commands():
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            n = len(await bot.tree.sync(guild=guild))
            log.info(f"[슬래시 등록] {guild.name}({guild.id}) {n}개")
        except Exception as e:
            log.warning(f"[슬래시 등록 실패] {guild.name}({guild.id}): {type(e).__name__}: {e}")


def _check_voice_libs():
    for mod in ("nacl.secret", "davey"):
        try:
            __import__(mod)
        except Exception as e:
            log.warning(f"[음성 라이브러리] {mod} 불러오기 실패 — 음성 재생 불가: {type(e).__name__}: {e}")
    try:
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        log.info(f"[음성 라이브러리] opus={'OK' if discord.opus.is_loaded() else '없음'} | ffmpeg={FFMPEG}")
    except Exception as e:
        log.warning(f"[음성 라이브러리] opus 불러오기 실패: {type(e).__name__}: {e}")


_ready_once = False


@bot.event
async def setup_hook():
    bot.add_view(ReplayView())
    _check_voice_libs()
    try:
        server = await asyncio.start_server(_ipc_handle, "127.0.0.1", IPC_PORT)
        asyncio.create_task(server.serve_forever())
        log.info(f"[IPC] 127.0.0.1:{IPC_PORT}")
    except OSError as e:   # 포트가 잠시 안 풀렸어도 TTS 자체는 동작하게
        log.warning(f"[IPC] 포트 {IPC_PORT} 사용 불가 — 음성 안내 IPC 없이 시작: {e}")


@bot.event
async def on_ready():
    global _ready_once
    log.info(f"[준비 완료] {bot.user} — 서버 {len(bot.guilds)}개 (/tts 내용 [목소리])")
    if _ready_once:
        return
    _ready_once = True
    await _ensure_tts_channel()
    if ALLOWED_CHANNEL_IDS:
        log.info(f"[채널] TTS 읽는 채널: {', '.join(str(c) for c in sorted(ALLOWED_CHANNEL_IDS))}")
    await _sync_slash_commands()


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        log.warning(f"[슬래시 동기화 실패] {guild.name}: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.id != bot.user.id or after.channel is not None:
        return
    st = _states.get(member.guild.id)   # 강제 퇴장/연결 끊김 → 대기열 정리
    if st:
        _cancel_idle(st)
        while not st.queue.empty():
            st.queue.get_nowait()
            st.queue.task_done()


def _read_token(name: str) -> str:
    try:
        with open(get_config_path(name), "r", encoding="utf-8") as f:
            return (json.load(f).get("token") or "").strip()
    except Exception:
        return ""


def _wait_for_token() -> str:
    """전용 토큰이 들어올 때까지 기다린다 (비어 있을 때 Watchdog 재시작 반복 방지).
    보스봇/음악봇과 같은 토큰이면 음성채널·슬래시 명령이 서로 덮이므로 시작하지 않는다."""
    last = None
    while True:
        own = _read_token("tts_config.json")
        others = {_read_token("boss_config.json"), _read_token("music_config.json")} - {""}
        if own and own not in others:
            return own
        reason = ("[대기] TTS 봇 토큰이 없습니다 — RaidBook 창 ⚙ → 설정 마법사 3단계(TTS Bot)나 Config/tts_config.json에 TTS 봇 토큰을 넣으면 자동으로 시작합니다."
                  if not own else
                  "[대기] TTS 토큰이 보스봇/음악봇 토큰과 같습니다 — 서버에 추가한 TTS 전용 봇의 토큰을 넣어 주세요.")
        if reason != last:
            log.info(reason)
            last = reason
        time.sleep(30)


def _wait_after_failure(token: str, seconds: int = 600):
    """시작 실패 후 바로 끝내면 Watchdog이 빠르게 재시작을 반복하다 감시를 멈추므로,
    토큰이 바뀌거나 일정 시간이 지날 때까지 기다렸다 종료한다 (재시작 시 다시 시도)."""
    end = time.time() + seconds
    while time.time() < end and _read_token("tts_config.json") == token:
        time.sleep(15)


if __name__ == "__main__":
    _token = _wait_for_token()
    try:
        bot.run(_token, log_handler=None)
    except discord.LoginFailure:
        log.error("[시작 실패] TTS 봇 토큰이 올바르지 않습니다. 토큰을 다시 복사해 넣어 주세요. 토큰을 바꾸면 바로 다시 시도합니다.")
        _wait_after_failure(_token)
        sys.exit(1)
