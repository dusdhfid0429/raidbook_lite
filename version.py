"""
RaidBook 버전 정보
상업용 배포를 위한 중앙화된 버전 관리
"""
from datetime import datetime

# 버전 정보
VERSION_MAJOR = 1
VERSION_MINOR = 0
VERSION_PATCH = 10
VERSION_BUILD = 1

# 버전 문자열
VERSION = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}"
VERSION_FULL = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}.{VERSION_BUILD}"

# 릴리즈 정보
RELEASE_DATE = "2026-10-28"
RELEASE_NAME = "Lite - 유찰 재경매 팝업"
RELEASE_CHANNEL = "stable"  # stable, beta, alpha

# 제품 정보
PRODUCT_NAME = "RaidBook"
PRODUCT_DESCRIPTION = "Discord 보스 알림·인원체크·경매 봇 (Lite)"
COMPANY_NAME = "RaidBook Team"
COPYRIGHT = f"Copyright © 2026 {COMPANY_NAME}. All rights reserved."

# 업데이트 정보
UPDATE_CHECK_URL = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
UPDATE_DOWNLOAD_URL = "https://github.com/{owner}/{repo}/releases/download/{tag}/{filename}"

# GitHub 설정
GITHUB_OWNER = "dusdhfid0429"
GITHUB_REPO = "raidbook_lite"

# 빌드 정보
BUILD_DATE = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
BUILD_TYPE = "Release"  # Release, Debug (main에 VERSION 변경 push 시 자동 릴리즈)


def get_version_info():
    """버전 정보를 딕셔너리로 반환"""
    return {
        "version": VERSION,
        "version_full": VERSION_FULL,
        "release_date": RELEASE_DATE,
        "release_name": RELEASE_NAME,
        "release_channel": RELEASE_CHANNEL,
        "product_name": PRODUCT_NAME,
        "company_name": COMPANY_NAME,
        "copyright": COPYRIGHT,
        "build_date": BUILD_DATE,
        "build_type": BUILD_TYPE,
    }


def get_version_string():
    """사용자에게 표시할 버전 문자열"""
    return f"{PRODUCT_NAME} v{VERSION} ({RELEASE_CHANNEL})"


def get_update_check_url():
    """업데이트 확인 URL 반환"""
    return UPDATE_CHECK_URL.format(
        owner=GITHUB_OWNER,
        repo=GITHUB_REPO
    )


def get_update_download_url(tag, filename):
    """업데이트 다운로드 URL 반환"""
    return UPDATE_DOWNLOAD_URL.format(
        owner=GITHUB_OWNER,
        repo=GITHUB_REPO,
        tag=tag,
        filename=filename
    )


if __name__ == "__main__":
    # 버전 정보 출력 (테스트용)
    print(f"{PRODUCT_NAME} Version Information")
    print("=" * 50)
    for key, value in get_version_info().items():
        print(f"{key:20s}: {value}")
