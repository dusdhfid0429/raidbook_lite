"""
RaidBook 자동 업데이트 모듈
GitHub Releases 기반 업데이트 확인 및 다운로드
"""
import os
import sys
import json
import logging
import requests
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict
from datetime import datetime
import version

log = logging.getLogger("RaidBook.Updater")


def load_github_token() -> Optional[str]:
    """
    Private repo 접근용 GitHub 토큰 로드.

    우선순위: 환경변수 RAIDBOOK_GITHUB_TOKEN → Config/github_token.txt (런타임 경로)
    """
    token = os.environ.get("RAIDBOOK_GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        from config_helper import get_config_path
        token_path = get_config_path("github_token.txt")
        if os.path.exists(token_path):
            with open(token_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
            # 템플릿 문구 그대로면 무시
            if token and not token.startswith("#") and " " not in token:
                return token
    except Exception as e:
        log.debug(f"github_token.txt 로드 실패: {e}")
    return None


class UpdateChecker:
    """GitHub Releases 기반 업데이트 확인"""

    def __init__(self, github_token: Optional[str] = None):
        """
        Args:
            github_token: Private repo인 경우 GitHub Personal Access Token
                          (미지정 시 env/Config에서 자동 로드)
        """
        if github_token is None:
            github_token = load_github_token()
        self.github_token = github_token
        self.current_version = version.VERSION
        self.headers = {}

        if github_token:
            self.headers["Authorization"] = f"token {github_token}"

    def check_for_updates(self) -> Tuple[bool, Optional[Dict]]:
        """
        최신 버전 확인

        Returns:
            (업데이트 가능 여부, 릴리즈 정보)
        """
        try:
            url = version.get_update_check_url()
            log.info(f"업데이트 확인 중: {url}")

            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 404:
                # 릴리즈가 아직 없는 repo — 업데이트 없음으로 처리
                log.info("릴리즈가 없습니다 (404) — 업데이트 없음")
                return False, None
            response.raise_for_status()

            release_data = response.json()
            latest_version = release_data["tag_name"].lstrip("v")

            log.info(f"현재 버전: {self.current_version}, 최신 버전: {latest_version}")

            if self._is_newer_version(latest_version, self.current_version):
                return True, {
                    "version": latest_version,
                    "tag_name": release_data["tag_name"],
                    "name": release_data.get("name", ""),
                    "body": release_data.get("body", ""),
                    "published_at": release_data.get("published_at", ""),
                    "assets": release_data.get("assets", []),
                    "html_url": release_data.get("html_url", ""),
                }
            else:
                return False, None

        except requests.exceptions.RequestException as e:
            log.error(f"업데이트 확인 실패: {e}")
            return False, None
        except Exception as e:
            log.error(f"업데이트 확인 중 오류: {e}")
            return False, None

    def _is_newer_version(self, latest: str, current: str) -> bool:
        """
        버전 비교 (semantic versioning)

        Args:
            latest: 최신 버전 (예: "1.2.3")
            current: 현재 버전 (예: "1.0.0")

        Returns:
            latest > current 여부
        """
        try:
            latest_parts = [int(x) for x in latest.split(".")]
            current_parts = [int(x) for x in current.split(".")]

            # 길이 맞추기
            max_len = max(len(latest_parts), len(current_parts))
            latest_parts += [0] * (max_len - len(latest_parts))
            current_parts += [0] * (max_len - len(current_parts))

            return latest_parts > current_parts
        except Exception as e:
            log.error(f"버전 비교 실패: {e}")
            return False

    def get_download_url(self, release_info: Dict) -> Optional[str]:
        """
        설치 파일 다운로드 URL 추출

        Args:
            release_info: check_for_updates()에서 반환된 릴리즈 정보

        Returns:
            다운로드 URL (없으면 None)
        """
        assets = release_info.get("assets", [])

        def _url(asset: Dict) -> Optional[str]:
            # Private repo는 browser_download_url이 인증을 못 받으므로
            # 토큰이 있으면 API asset URL(Accept: octet-stream) 사용
            if self.github_token and asset.get("url"):
                return asset.get("url")
            return asset.get("browser_download_url")

        # Windows 인스톨러 찾기
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".exe") and "setup" in name:
                return _url(asset)

        # .exe 파일 찾기
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".exe"):
                return _url(asset)

        return None


class UpdateDownloader:
    """업데이트 파일 다운로드"""

    def __init__(self, github_token: Optional[str] = None):
        if github_token is None:
            github_token = load_github_token()
        self.github_token = github_token
        self.headers = {}

        if github_token:
            self.headers["Authorization"] = f"token {github_token}"

    def download(self, url: str, progress_callback=None) -> Optional[str]:
        """
        업데이트 파일 다운로드

        Args:
            url: 다운로드 URL
            progress_callback: 진행률 콜백 함수 (received_bytes, total_bytes)

        Returns:
            다운로드된 파일 경로 (실패 시 None)
        """
        try:
            log.info(f"다운로드 시작: {url}")

            headers = dict(self.headers)
            if "api.github.com" in url:
                headers["Accept"] = "application/octet-stream"

            response = requests.get(url, headers=headers, stream=True, timeout=30)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))

            # 임시 파일로 다운로드
            fd, temp_path = tempfile.mkstemp(suffix=".exe")
            os.close(fd)

            downloaded = 0
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size)

            log.info(f"다운로드 완료: {temp_path}")
            return temp_path

        except Exception as e:
            log.error(f"다운로드 실패: {e}")
            return None


class UpdateInstaller:
    """업데이트 설치"""

    @staticmethod
    def install(installer_path: str, silent: bool = False) -> bool:
        """
        인스톨러 실행

        Args:
            installer_path: 인스톨러 파일 경로
            silent: 자동 설치 모드 여부

        Returns:
            실행 성공 여부
        """
        try:
            log.info(f"인스톨러 실행: {installer_path}")

            args = [installer_path]
            if silent:
                args.append("/SILENT")  # Inno Setup silent mode

            # 인스톨러 실행 후 현재 프로그램 종료
            subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)

            log.info("인스톨러 실행됨. 현재 프로그램 종료 예정.")
            return True

        except Exception as e:
            log.error(f"인스톨러 실행 실패: {e}")
            return False


def create_backup(backup_dir: Optional[str] = None) -> bool:
    """
    현재 설치본 백업

    Args:
        backup_dir: 백업 디렉토리 (기본: 현재 디렉토리/Backup)

    Returns:
        백업 성공 여부
    """
    try:
        if backup_dir is None:
            backup_dir = os.path.join(os.path.dirname(sys.executable), "Backup")

        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"RaidBook_{version.VERSION}_{timestamp}"
        backup_path = os.path.join(backup_dir, backup_name)

        log.info(f"백업 생성 중: {backup_path}")

        # 현재 디렉토리 백업 (Log 폴더 제외)
        current_dir = os.path.dirname(sys.executable)
        shutil.copytree(
            current_dir,
            backup_path,
            ignore=shutil.ignore_patterns("Log", "*.log", "__pycache__", "Backup")
        )

        log.info(f"백업 완료: {backup_path}")
        return True

    except Exception as e:
        log.error(f"백업 실패: {e}")
        return False


# ── 사용 예제 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    # 업데이트 확인
    checker = UpdateChecker()
    has_update, release_info = checker.check_for_updates()

    if has_update:
        print(f"\n새 버전 발견: {release_info['version']}")
        print(f"릴리즈 노트:\n{release_info['body']}\n")

        download_url = checker.get_download_url(release_info)
        if download_url:
            print(f"다운로드 URL: {download_url}")

            # 다운로드
            downloader = UpdateDownloader()

            def progress(received, total):
                percent = (received / total) * 100
                print(f"\r다운로드 진행률: {percent:.1f}%", end="")

            installer_path = downloader.download(download_url, progress)

            if installer_path:
                print(f"\n\n다운로드 완료: {installer_path}")

                # 백업
                if create_backup():
                    print("백업 완료")

                # 설치
                print("인스톨러를 실행하시겠습니까? (y/n): ", end="")
                if input().lower() == "y":
                    UpdateInstaller.install(installer_path)
    else:
        print("최신 버전을 사용 중입니다.")
