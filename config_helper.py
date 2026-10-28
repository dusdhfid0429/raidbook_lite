"""
RaidBook Config Helper
템플릿에서 실제 설정 파일을 자동 생성하는 유틸리티
"""
import os
import shutil
import sys
import json


APP_NAME = "RaidBook"


def _safe_print(msg: str, file=None):
    """cp949 등 이모지를 못 찍는 콘솔에서도 예외 없이 출력."""
    out = file or sys.stdout
    if out is None:                     # pythonw 등 콘솔 없음
        return
    try:
        print(msg, file=out)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc), file=out)
    except Exception:
        pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_app_dir() -> str:
    """Return the installed executable directory, or the source directory in dev."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_bundle_dir() -> str:
    """Return PyInstaller's extraction dir when present, otherwise the app dir."""
    return os.path.abspath(getattr(sys, "_MEIPASS", get_app_dir()))


def get_runtime_dir() -> str:
    """
    Return a writable runtime directory.

    Installed Windows apps should not write under Program Files. In frozen mode
    keep Config/Data/Log under ProgramData/RaidBook; in dev keep repo-local paths.
    """
    if is_frozen() and os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA") or os.path.join(
            os.environ.get("SystemDrive", "C:"), "ProgramData"
        )
        return os.path.join(program_data, APP_NAME)
    return get_app_dir()


def runtime_path(*parts: str) -> str:
    return os.path.join(get_runtime_dir(), *parts)


def get_config_path(filename: str) -> str:
    return runtime_path("Config", filename)


def _candidate_template_paths(config_path: str):
    base, ext = os.path.splitext(config_path)
    template_name = os.path.basename(f"{base}.template{ext}")
    config_dir = os.path.dirname(config_path)
    return [
        os.path.join(config_dir, template_name),
        os.path.join(get_app_dir(), "Config", template_name),
        os.path.join(get_bundle_dir(), "Config", template_name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "Config", template_name),
    ]


def ensure_runtime_tree():
    """Create writable runtime folders and seed bundled runtime files once."""
    runtime_dir = get_runtime_dir()
    os.makedirs(runtime_dir, exist_ok=True)
    for folder in ("Config", "Data", "Log"):
        os.makedirs(os.path.join(runtime_dir, folder), exist_ok=True)

    if not is_frozen():
        return

    for folder in ("Data", "Images"):
        dst = os.path.join(runtime_dir, folder)
        if folder == "Data" and os.path.exists(dst) and os.listdir(dst):
            continue
        for root in (get_app_dir(), get_bundle_dir()):
            src = os.path.join(root, folder)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
                break


def _patch_default_base_dir(config_path: str):
    if not config_path.lower().endswith(".json"):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "base_dir" in data and not data.get("base_dir"):
            data["base_dir"] = get_runtime_dir()
            tmp_path = f"{config_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
                f.write("\n")
            os.replace(tmp_path, config_path)
    except Exception as e:
        _safe_print(f"⚠ base_dir 기본값 설정 실패: {config_path} ({e})", file=sys.stderr)


def ensure_config_from_template(config_path: str) -> bool:
    """
    설정 파일이 없으면 템플릿에서 생성

    Args:
        config_path: 설정 파일 경로 (예: "Config/boss_config.json")

    Returns:
        bool: True=기존 파일 존재 또는 생성 성공, False=템플릿 없음
    """
    # 이미 설정 파일이 있으면 통과
    if os.path.exists(config_path):
        return True

    ensure_runtime_tree()

    # 템플릿이 없으면 실패
    template_path = None
    for candidate in _candidate_template_paths(config_path):
        if os.path.exists(candidate):
            template_path = candidate
            break
    if template_path is None:
        _safe_print(f"❌ 템플릿 파일 없음: {config_path}", file=sys.stderr)
        for candidate in _candidate_template_paths(config_path):
            _safe_print(f"   확인 경로: {candidate}", file=sys.stderr)
        return False

    # 템플릿에서 설정 파일 생성
    try:
        # Config 폴더가 없으면 생성
        config_dir = os.path.dirname(config_path)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)

        # 템플릿 복사
        shutil.copy2(template_path, config_path)
        _patch_default_base_dir(config_path)
        _safe_print(f"✅ 설정 파일 생성됨: {config_path}")
        _safe_print(f"   (템플릿: {template_path})")
        return True

    except Exception as e:
        _safe_print(f"❌ 설정 파일 생성 실패: {config_path}", file=sys.stderr)
        _safe_print(f"   오류: {e}", file=sys.stderr)
        return False


def check_required_configs(config_files: list[str]) -> bool:
    """
    필수 설정 파일들 확인 및 생성

    Args:
        config_files: 확인할 설정 파일 경로 목록

    Returns:
        bool: 모든 설정 파일 존재 여부
    """
    all_ok = True

    for config_file in config_files:
        if not ensure_config_from_template(config_file):
            all_ok = False

    return all_ok


if __name__ == "__main__":
    # 테스트: 모든 설정 파일 확인
    configs = [
        get_config_path("boss_config.json"),
        get_config_path("combat_config.json"),
        get_config_path("accountant_config.json"),
        get_config_path("tts_config.json"),
        get_config_path("watchdog_config.json"),
        get_config_path("BotToken.txt"),
    ]

    print("=" * 60)
    print("RaidBook 설정 파일 확인")
    print("=" * 60)

    if check_required_configs(configs):
        print("\n✅ 모든 설정 파일 확인 완료")
    else:
        print("\n❌ 일부 설정 파일 생성 실패")
        sys.exit(1)
