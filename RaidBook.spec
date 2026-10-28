# -*- mode: python ; coding: utf-8 -*-
"""RaidBook Lite — Watchdog GUI(RaidBook.exe) + Boss Bot(RaidBook_Boss_Bot.exe) + Music Bot(RaidBook_Music_Bot.exe) 빌드"""
import os
from PyInstaller.utils.hooks import collect_data_files

DATAS = [
    ('Config', 'Config'),
    ('Images', 'Images'),
    ('version.py', '.'),
    ('config_helper.py', '.'),
]
EXCLUDES = ['matplotlib', 'numpy', 'pandas', 'scipy', 'IPython', 'jupyter', 'edge_tts']
NO_VOICE = ['nacl', 'davey']   # 음성 재생은 Music Bot에만 필요
ICON = 'Images/Icon/WatchDog.ico' if os.path.exists('Images/Icon/WatchDog.ico') else None
VERSION_FILE = 'version_info.txt' if os.path.exists('version_info.txt') else None


def build(script, name, hidden, voice=False):
    datas = DATAS + (collect_data_files('discord', includes=['bin/*']) if voice else [])
    a = Analysis([script], pathex=[os.path.abspath(SPECPATH)], datas=datas,
                 hiddenimports=hidden, excludes=EXCLUDES + ([] if voice else NO_VOICE), noarchive=False)
    pyz = PYZ(a.pure, a.zipped_data)
    # 설치판 부트로더는 PYTHONUTF8 환경변수를 무시하므로 UTF-8 모드를 빌드 옵션으로 고정 (로그 한글 깨짐 방지)
    return EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [('X utf8_mode=1', None, 'OPTION')], name=name,
               debug=False, strip=False, upx=False, console=False,
               icon=ICON, version_file=VERSION_FILE)


build('RaidBook_Watchdog_GUI.py', 'RaidBook', [
    'discord', 'discord.ext.commands', 'pystray', 'PIL', 'PIL.Image',
    'tkinter', 'tkinter.ttk', 'tkinter.scrolledtext', 'tkinter.messagebox',
    'RaidBook_Watchdog', 'RaidBook_updater', 'RaidBook_channel_setup', 'RaidBook_common', 'version', 'config_helper',
])
build('RaidBook_boss_Bot.py', 'RaidBook_Boss_Bot', [
    'discord', 'discord.ext.commands', 'openpyxl', 'PIL', 'PIL.Image',
    'RaidBook_common', 'RaidBook_storage', 'RaidBook_store_core', 'RaidBook_store_attend',
    'RaidBook_users', 'RaidBook_ipc', 'RaidBook_boss_data', 'RaidBook_images', 'RaidBook_loot',
    'RaidBook_attendance', 'RaidBook_members', 'RaidBook_party', 'RaidBook_schedule',
    'RaidBook_boss_auction', 'RaidBook_channel_setup', 'RaidBook_music', 'config_helper',
])
# 음악 봇 — 음성(PyNaCl·davey·opus) 포함. yt-dlp/deno는 첫 실행 시 Tools 폴더로 받고, ffmpeg.exe는 build.ps1이 dist에 복사
build('RaidBook_Music_Bot.py', 'RaidBook_Music_Bot', [
    'discord', 'discord.ext.commands', 'discord.voice_client', 'nacl', 'nacl.secret', 'nacl.utils', 'davey',
    'RaidBook_music', 'RaidBook_storage', 'RaidBook_common', 'config_helper',
], voice=True)
