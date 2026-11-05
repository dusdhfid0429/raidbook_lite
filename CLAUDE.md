# CLAUDE.md

RaidBook Lite — 보스 알림/인원체크/경매만 남긴 경량판. 원본(상업용 전체판)은 raidbook_dev.

## 구성
- `RaidBook_Watchdog_GUI.py` → `RaidBook_Watchdog.py`(봇 감시, Discord 관리 채널 명령, `!panel` 원격 패널) → `RaidBook_boss_Bot.py`
- `RaidBook_Music_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_music`) — /재생 유튜브 스트리밍. token 비면 boss 토큰 공유 → 슬래시 명령은 upsert로만 등록, 보스봇 sync는 `sync_guild_keeping`으로 음악 명령 유지 (명령 추가 시 `MUSIC_COMMAND_NAMES` 갱신). 동시 재생: `extra_tokens`의 봇들을 같은 프로세스에서 음성 전용 `discord.Client`로 띄우고, 명령·버튼·패널은 메인 봇이 처리. 플레이어는 (봇, 서버)마다 따로 → `rbm.pick_bot`으로 배정(같은 채널 봇 → 빈 봇 → 노는 봇), 조작 대상은 패널 메시지 → 사용자 음성채널 순으로 찾음. extra_tokens가 있으면 메인(보스 토큰) 봇은 음성에 안 들어가고 명령·패널만
- `RaidBook_TTS_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_tts`) — `/tts 내용 [목소리]`를 edge_tts로 읽고 채널에 글 + 🔄 다시 듣기(persistent, 메시지 내용 재해석) 남김. **전용 토큰 필수**(보스/음악 토큰과 같으면 시작 안 함). `tts` 채널 자동 생성, 토큰 비면 대기만. 설정 마법사 3단계(선택), 추가 음악봇 토큰은 4단계(선택)
- 보스봇 모듈: boss_auction(경매), combat_support(/전투지원 — 15자 팝업 → 전투알림 채널 등록 + 대화방 TTS(IPC speak, `combat_support.tts_channel_ids` 개수만큼) + 전투방 링크·인원체크·➕/➖, 채널은 boss_config `combat_alert_channel_id`·`combat_support.clans`), loot(전리품 공지), attendance, boss_data, members, party, schedule, store_*, storage, users, images, ipc, common
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
python Tests/test_manual_close_burst.py
python Tests/test_music.py
python Tests/test_tts.py
python Tests/test_clans.py
python Tests/test_combat_support.py
python -m pyflakes *.py
```

## 릴리즈
`version.py` 버전 올려 main에 push → Actions(`.github/workflows/release.yml`)가 설치파일을 Release에 업로드 → 설치판은 업데이트 버튼으로 받음.
