# CLAUDE.md

RaidBook Lite — 보스 알림/인원체크/경매만 남긴 경량판. 원본(상업용 전체판)은 raidbook_dev.

## 구성
- `RaidBook_Watchdog_GUI.py` → `RaidBook_Watchdog.py`(봇 감시, Discord 관리 채널 명령, `!panel` 원격 패널) → `RaidBook_boss_Bot.py`
- `RaidBook_Music_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_music`) — /재생 유튜브 스트리밍. token 비면 boss 토큰 공유 → 슬래시 명령은 upsert로만 등록, 보스봇 sync는 `sync_guild_keeping`으로 음악 명령 유지 (명령 추가 시 `MUSIC_COMMAND_NAMES` 갱신)
- `RaidBook_TTS_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_tts`) — 채팅 `#텍스트`/`#1`(남)/`#2`(여)를 edge_tts로 읽음. **전용 토큰 필수**(보스/음악 토큰과 같으면 시작 안 함), Message Content Intent 필요. `tts` 채널 자동 생성, 토큰 비면 대기만. 설정 마법사 3단계(선택)
- 보스봇 모듈: boss_auction(경매), loot(전리품 공지), attendance, boss_data, members, party, schedule, store_*, storage, users, images, ipc, common
- 런타임 경로: 소스 실행=저장소 폴더, 설치판=`C:\ProgramData\RaidBook` (`config_helper.get_runtime_dir`)

## 규칙
- 공개 embed에는 이름·상태만. 금액 상세는 관리자/본인 ephemeral
- JSON 저장은 `write_json_atomic`, Excel은 `save_workbook_safely(_sync)`
- persistent View `custom_id`는 재시작 복구와 충돌 없게 고정
- 실제 토큰/채널 Config는 Git 제외(`*.template.json`만 커밋)

## 검증
```bash
python Tests/test_boss_auction.py
python Tests/test_boss_board.py
python Tests/test_bid_burst.py
python Tests/test_music.py
python Tests/test_tts.py
python -m pyflakes *.py
```

## 릴리즈
`version.py` 버전 올려 main에 push → Actions(`.github/workflows/release.yml`)가 설치파일을 Release에 업로드 → 설치판은 업데이트 버튼으로 받음.
