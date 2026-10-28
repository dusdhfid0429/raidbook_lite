# RaidBook Lite

Discord 보스 알림 · 음성채널 인원체크 · 전리품 경매 봇 (Watchdog + Boss Bot).

## 기능
- 보스 알림 메시지(정각/일일 반복) + 🎙️ 인원체크 버튼(관리자) → 음성채널 접속자를 참석자로 기록
- 💎 득템·경매: 알림 메시지에서 경매할 아이템(줄마다 시작가·호가·카운트, xN 수량)과 기록만 할 아이템을 한 번에 입력
  → 득템 기록 + 경매장에 **진행판** 하나가 올라감 — 아이템마다 관리자가 ▶️ 경매시작, 참석자는 🙋 입찰만
  → 아이템별 낙찰자·낙찰가가 진행판과 보스 알림 메시지에 남음
- 필요한 채널(보탐-알림·보탐-명령·경매장·raidbook-관리)이 없으면 시작 시 'RaidBook' 카테고리에 자동 생성 (봇에 '채널 관리' 권한 필요)
- Watchdog: 봇 자동 재시작, 관리 채널 `!panel` 원격 제어 패널(상태·시작·중지·재시작·로그·업데이트)

## 설치 / 업데이트
1. [Releases](../../releases)에서 `RaidBook_Setup_x.y.z.exe` 실행 (관리자 권한 불필요)
2. 설정·데이터 위치: `C:\ProgramData\RaidBook\` (Config, Data, Log) — 업데이트해도 유지
3. 비공개 저장소이므로 업데이트 확인용 토큰을 `C:\ProgramData\RaidBook\Config\github_token.txt`에 넣는다 (Contents: Read-only 권한의 fine-grained PAT)
4. 업데이트: GUI 🔄 버튼 또는 Discord `!panel` → ⬆️ 업데이트 (무인 설치 후 자동 재실행)

## 릴리즈 (개발자)
1. `version.py`의 `VERSION_*` 올리기
2. main에 push (version.py 변경이 감지되면 자동 빌드)
3. GitHub Actions가 Windows에서 빌드 → Release에 설치파일 업로드

## 소스 실행
```powershell
pip install -r requirements.txt
python RaidBook_Watchdog_GUI.py   # 또는 RaidBook_Watchdog_GUI.vbs (콘솔 숨김)
```
