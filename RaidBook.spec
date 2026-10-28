# -*- mode: python ; coding: utf-8 -*-
"""RaidBook Lite — Watchdog GUI(RaidBook.exe) + Boss Bot(RaidBook_Boss_Bot.exe) 빌드"""
import os

DATAS = [
    ('Config', 'Config'),
    ('Images', 'Images'),
    ('version.py', '.'),
    ('config_helper.py', '.'),
]
EXCLUDES = ['matplotlib', 'numpy', 'pandas', 'scipy', 'IPython', 'jupyter', 'nacl', 'edge_tts']
ICON = 'Images/Icon/WatchDog.ico' if os.path.exists('Images/Icon/WatchDog.ico') else None
VERSION_FILE = 'version_info.txt' if os.path.exists('version_info.txt') else None


def build(script, name, hidden):
    a = Analysis([script], pathex=[os.path.abspath(SPECPATH)], datas=DATAS,
                 hiddenimports=hidden, excludes=EXCLUDES, noarchive=False)
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
    'RaidBook_boss_auction', 'RaidBook_channel_setup', 'config_helper',
])
