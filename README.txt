RaidBook Lite
=============
Discord 보스 알림 · 인원체크 · 전리품 경매 봇

■ 설정·데이터 위치
  C:\ProgramData\RaidBook\  (Config, Data, Log)
  첫 실행 시 설정 마법사가 Watchdog/Boss Bot 토큰과 채널을 묻습니다.

■ 음악 (Music Bot)
  음성채널에 들어간 뒤 /재생 <유튜브 링크> — 보스봇 토큰을 같이 쓰므로 따로 설정할 것 없음.
  /건너뛰기 /일시정지 /정지 /대기열 /볼륨, 설정: Config\music_config.json
  여러 채널 동시 재생: 설정 마법사 4단계(또는 music_config.json의 extra_tokens)에 음악 전용 봇 토큰을 추가하면
  넣은 수만큼 봇이 늘어나고, 채널마다 대기열이 따로 돌아갑니다.

■ 원격 제어
  Watchdog 관리 채널에서 !panel 입력 → 상태·시작·중지·재시작·로그·업데이트 버튼

■ 업데이트
  Config\github_token.txt 에 GitHub 토큰을 넣으면 GUI 🔄 버튼이나 !panel 업데이트 버튼으로 설치됩니다.
