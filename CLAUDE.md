# CLAUDE.md

RaidBook Lite — 보스 알림/인원체크/경매만 남긴 경량판. 원본(상업용 전체판)은 raidbook_dev.

## 구성
- `RaidBook_Watchdog_GUI.py` → `RaidBook_Watchdog.py`(봇 감시, Discord 관리 채널 명령, `!panel` 원격 패널) → `RaidBook_boss_Bot.py`
- `RaidBook_Music_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_music`) — /재생 유튜브 스트리밍. token 비면 boss 토큰 공유 → 슬래시 명령은 upsert로만 등록, 보스봇 sync는 `sync_guild_keeping`으로 음악 명령 유지 (명령 추가 시 `MUSIC_COMMAND_NAMES` 갱신). 동시 재생: `extra_tokens`의 봇들을 같은 프로세스에서 음성 전용 `discord.Client`로 띄우고, 명령·버튼·패널은 메인 봇이 처리. 플레이어는 (봇, 서버)마다 따로 → `rbm.pick_bot`으로 배정(같은 채널 봇 → 빈 봇 → 노는 봇), 조작 대상은 패널 메시지 → 사용자 음성채널 순으로 찾음. extra_tokens가 있으면 메인(보스 토큰) 봇은 음성에 안 들어가고 명령·패널만
- `RaidBook_TTS_Bot.py`(별도 프로세스, 공용 로직 `RaidBook_tts`) — `/tts 내용 [목소리]`를 edge_tts로 읽고 채널에 글 + 🔄 다시 듣기(persistent, 메시지 내용 재해석) 남김. **전용 토큰 필수**(보스/음악 토큰과 같으면 시작 안 함). `tts` 채널 자동 생성, 토큰 비면 대기만. 설정 마법사 3단계(선택), 추가 음악봇 토큰은 4단계(선택)
- 보스봇 모듈: boss_auction(경매), combat_support(/전투지원 — 15자 팝업 → 전투알림 채널 등록 + 대화방 음성 안내(`combat_support.tts_channel_ids` 개수만큼 — 음악봇 IPC announce(7705)로 방마다 뮤직봇 하나씩 동시에, 재생 중이면 재생 종료 후 사용, `rbm.assign_announce`; 음악봇 꺼지면 TTS 봇 speak로 차례로) + 전투방 링크·인원체크·➕/➖·⏹️ 전투종료(운영진·요청자, 마지막 인원체크 후 회색·인원체크 끔), 화면은 B안(제목 고정·📌 내용), 채널은 boss_config `combat_alert_channel_id`·`combat_support.clans`), loot(전리품 공지), attendance, boss_data, members, party, schedule, store_*, storage, users, images, ipc, common
- /보스알림(`RaidBook_boss_manual`) — 운영진만, 보스알림 채널에서만. 팝업(보스 선택 목록 + 직접 입력 + 젠 일시 12자리 `202611071700`) → 자동 알림과 같은 `_post_boss_alert`로 게시(화면·버튼·인원체크·마감·저장 동일). 2시간 넘게 남은/24시간 넘게 지난 일시는 거부, 지난 일시는 음성방 인원체크 없이 바로 마감(➕ 인원 추가로 기록). 같은 보스·일시가 진행 중이면 안 만듦
- /전파(`RaidBook_broadcast`) — 운영진(`broadcast.role_name`='운영진' 역할)만, `broadcast.channel_ids` 채널에서만. 내용(50자)을 채널에 📢로 남기고 전투지원과 같은 대화방(`combat_support.tts_channel_ids`)에 `CombatSupport.speak`로 음성 전달 ("닉네임님 전달. 내용")
- 🔐 서버 입장 인증(`RaidBook_verify`) — `#인증` 채널(`verify_channel_id`, 비면 자동 생성)에 안내 메시지(B안: 🔐 서버 입장 인증 + 1·2 안내 + ⚠️ N회 틀리면 추방) + 🔑 비밀번호 입력 버튼(persistent `rb_verify:open`) → 모달 → 결과는 ephemeral. 맞으면 `verify.role_id`(비면 `role_name`='인증됨' 찾거나 생성) 부여, `verify.max_failures`(5)회 틀리면 추방(`fail_action`=kick). 틀린 횟수·안내 메시지 ID는 `Data/verify_state.json`. 첫 시작 때 기존 멤버 일괄 인증 1회(`_verify_existing_granted`). 봇은 시작 때마다 인증됨 부여. `/인증권한`(서버 관리 권한자, `RaidBook_verify_perms`): 모든 카테고리·채널을 @everyone 보기 끔 + 인증됨 보기 켬(원래 비공개 채널은 열지 않음), 다른 역할 권한 삭제(`verify.keep_role_name`='운영진' 역할·managed 역할·멤버 개별 권한은 유지), #인증은 @everyone만. ephemeral 미리보기 → ✅ 적용, 적용 전 `Data/verify_perm_backup_*.json` 백업 → `/인증권한 복구`. 기존 멤버 일괄 인증 전엔 거부
- 런타임 경로: 소스 실행=저장소 폴더, 설치판=`C:\ProgramData\RaidBook` (`config_helper.get_runtime_dir`)

## 규칙
- 🛡️ 권한은 `RaidBook_perms` 표 하나로 모든 프로그램(보스·음악·TTS·Watchdog)의 모든 기능을 등록: 명령은 `COMMANDS[봇][이름]`, 상시 버튼은 `COMPONENTS[custom_id]`. 타입 = NONE(누구나, 편의 기능)/운영진/서버주인(프로그램 조작: 스케줄·저장·재시작·데이터 수정·/인증권한·Watchdog 전부)/운영진·요청자/참석자. 운영진 = 서버 주인 또는 정확히 '운영진' 역할(`admin_role_names`·`admin_role_ids`), ○○운영진·서버 관리 권한은 운영진 아님. 입구: 각 봇 `tree.interaction_check = command_gate(봇)`, Watchdog `@_wdog_bot.check`, 버튼 `GuardedView`(표에 없는 공개 1회용 버튼은 `default_level`). 다른 프로세스는 `configure_from_file(boss_config.json)`. 명령·버튼 추가 시 표에 꼭 추가(`Tests/test_perms.py` 누락 검사), `permission_overrides`로 변경 가능. 보스알림 참석/취소 버튼 없음(음성 인원체크만)
- 공개 embed에는 이름·상태만. 금액 상세는 관리자/본인 ephemeral
- JSON 저장은 `write_json_atomic`, Excel은 `save_workbook_safely(_sync)`
- 보스 참석/득템 데이터 원본은 JSON(`Data/store/attend/RaidBook_Boss_Data.json`, `RaidBook_store_attend` 원본 모드, 롤링 백업 `Data/Backup/Boss/*.json`). 평소 저장은 JSON만(`_boss_store()`), 엑셀은 /save·/refresh·일일 정리 때 `export_boss_excel_sync`/`_tidy_and_export_sync`로 만들어 PC 파일 최신화·디스코드 전송. 처음 실행 때 기존 엑셀을 한 번 이전. 관리 명령은 `_wb_from_store()`로 통합문서를 만들어 고친 뒤 `_commit_wb()`로 반영
- 이벤트 루프에서 파일 대량 I/O 금지 — 무거운 작업은 `asyncio.to_thread`. 봇이 1초 넘게 멈추면 로그에 `[응답 지연]`
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
python Tests/test_broadcast.py
python Tests/test_boss_manual.py
python Tests/test_responsiveness.py   # 저장이 버튼 응답(이벤트 루프)을 멈추지 않는지
python Tests/test_boss_store.py       # JSON 원본 저장·엑셀 이전/내보내기
python Tests/test_verify.py
python Tests/test_verify_perms.py
python Tests/test_perms.py
python -m pyflakes *.py
```

## 릴리즈
`version.py` 버전 올려 main에 push → Actions(`.github/workflows/release.yml`)가 설치파일을 Release에 업로드 → 설치판은 업데이트 버튼으로 받음.
