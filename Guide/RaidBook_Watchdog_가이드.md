# 🐕 LinC Watchdog 가이드

> **파일**: `RaidBook_Watchdog.py`
> **Config**: `Config/watchdog_config.json`
> **로그**: `Log/RaidBook_Watchdog_YYYY-MM-DD.log`

---

## 📋 개요

모든 LinC 봇 프로세스를 감시하고 자동으로 재시작하는 관리자 프로그램입니다.
하나의 CMD 창에서 모든 봇을 동시에 관리하고, 선택적으로 제어할 수 있습니다.

### 기존 실행 방식과 비교

| 방식 | 명령 | 특징 |
|------|------|------|
| 기존 | 봇마다 CMD 창 4개 열어서 개별 실행 (`python RaidBook_boss_Bot.py` 등) | 창 관리 번거로움, 크래시 시 수동 재시작 |
| Watchdog | `python RaidBook_Watchdog.py` | CMD 창 1개로 전체 관리 + 자동 재시작 + 선택적 제어 |

---

## ⚙️ config 설정 (`watchdog_config.json`)

```json
{
  "base_dir": "C:/Users/.../RaidBook_Bot",
  "restart_delay_seconds": 5,
  "max_rapid_restarts": 5,
  "rapid_restart_window_seconds": 60,
  "network_check": {
    "enabled": true,
    "interval_seconds": 60,
    "timeout_seconds": 5,
    "connect_attempts": 2,
    "targets": [
      { "name": "Discord Gateway", "host": "gateway.discord.gg", "port": 443 },
      { "name": "Discord API", "host": "discord.com", "port": 443 }
    ]
  },
  "bots": [
    { "name": "회계봇", "script": "RaidBook_Accountant_Bot.py", "enabled": true },
    { "name": "보스봇", "script": "RaidBook_boss_Bot.py", "enabled": true },
    { "name": "전투봇", "script": "RaidBook_Combat_Bot.py", "enabled": true },
    { "name": "TTS봇",  "script": "RaidBook_TTS_Bot.py",  "enabled": true }
  ]
}
```

| 항목 | 설명 |
|------|------|
| `base_dir` | LinC 봇 루트 경로 |
| `restart_delay_seconds` | 봇 재시작 전 대기 시간 (초) |
| `max_rapid_restarts` | 빠른 재시작 제한 횟수 |
| `rapid_restart_window_seconds` | 빠른 재시작 감지 기간 (초) |
| `network_check` | Discord TCP 연결 상태 주기 확인 설정 |
| `bots` | 감시할 봇 목록 |

### bots 항목

| 항목 | 설명 |
|------|------|
| `name` | Watchdog 내부에서 사용하는 봇 식별 이름 |
| `script` | 실행할 Python 파일명 (Watchdog과 같은 폴더 기준) |
| `enabled` | `true`면 시작 시 자동 실행, `false`면 수동 시작 필요 |

---

## 🚀 실행 방법

```bash
python RaidBook_Watchdog.py
```

실행하면 `enabled: true`인 봇들이 자동으로 시작됩니다.

---

## 💬 Watchdog 콘솔 명령어

Watchdog 실행 후 콘솔에서 아래 명령어를 입력합니다.

| 명령어 | 설명 | 예시 |
|--------|------|------|
| `status` | 모든 봇 상태 확인 | `status` |
| `list` | 등록된 봇 목록 확인 | `list` |
| `start [봇이름]` | 중지된 봇 시작 | `start 회계봇` |
| `start all` | 모든 봇 시작 | `start all` |
| `stop [봇이름]` | 특정 봇 중지 | `stop TTS봇` |
| `stop all` | 모든 봇 중지 | `stop all` |
| `restart [봇이름]` | 특정 봇 재시작 | `restart 보스봇` |
| `restart all` | 모든 봇 재시작 | `restart all` |
| `netcheck` | Discord Gateway/API TCP 연결 즉시 확인 | `netcheck` |
| `help` | 도움말 | `help` |
| `quit` / `exit` | Watchdog 종료 (모든 봇 중지) | `quit` |

> 봇 이름은 **부분 매칭** 지원 — `회계`, `보스` 등 일부만 입력해도 됩니다.

---

## 💬 Watchdog Discord 명령어

Watchdog 자체도 Discord 봇으로 동작하며, 다른 봇들을 디스코드에서 제어할 수 있습니다.

### `!watchdog` 직접 명령

| 명령 | 설명 |
|---|---|
| `!watchdog status` | 모든 봇 상태 확인 |
| `!watchdog restart [봇\|all] [silent]` | 재시작 (kill+respawn). `silent` 입력 시 진행/완료 메시지 생략 |
| `!watchdog stop [봇\|all] [silent]` | 중지 |
| `!watchdog start [봇\|all] [silent]` | 시작 |
| `!watchdog help [boss\|combat\|account]` | 각 봇별 forward 명령어 목록 |

> 봇 이름: `boss` / `combat` / `account` / `tts` / `all`
> silent 모드여도 watchdog 로그에는 항상 기록됨 (`[wd restart all] silent=True`)

### `!boss` / `!combat` / `!account` Forward 명령

각 봇으로 명령을 전달하는 forward 라우터. **2가지 처리 방식**으로 동작합니다:

| 처리 방식 | 대상 명령 | 동작 |
|---|---|---|
| **IPC 전달** | 대부분의 명령 | watchdog → 각 봇의 IPC 서버 → 결과 다시 watchdog → Discord 응답 |
| **일부 로컬 처리 + IPC** | `!account settle`, `!account 미지급`, `!account 조회`, `!account 분배파일`, `!account 지급처리` | Watchdog은 명령 접수/상태 메시지를 담당하고, 정산 패널 생성과 View/Modal 동작은 Accountant Bot IPC/프로세스가 담당 |

> Accountant View/Modal 구현은 `RaidBook_views_account.py`로 분리되어 있으며, 정산 버튼 변경 시 Accountant Bot 재시작이 필요합니다.

### Boss Bot Forward (`!boss ...`)
주요 명령:
- 알람: `listboss`, `startboss`, `stopboss`, `setboss`
- 메시지 관리: `create`, `close`, `activate`, `extend`, `reopen`, `rename`, `recover`, `sync`, `delete`
- 데이터: `history`, `stats [일수\|all] [상위N\|all]`, `save`, `refresh`, `fixid`, `migrate`, `collectmembers`, `test`
- 버튼: `attachloot [메시지ID\|all] [일수]` — 기존 메시지에 획득 아이템 버튼 부착
- 운영: `restart [silent]`

### Combat Bot Forward (`!combat ...`)
주요 명령:
- 메시지 관리: `close`, `activate`, `extend`, `reopen`, `rename`, `recover`, `sync`, `delete`
- 데이터: `history`, `stats [일수\|all] [상위N\|all]`, `save`, `refresh`, `fixid`
- 운영: `restart [silent]`

### Accountant Bot Forward (`!account ...`)
주요 명령:
- 거래: `잔액`, `내역`, `입금`, `출금`, `검색`, `수정`
- 정산: `settle [YYMMDDHH]`, `미지급`, `조회 [멤버명]`, `지급처리`, `setrole`, `setpct`, `분배파일`
- 데이터: `sync`, `save`, `refresh`, `setrefresh`
- 운영: `restart [silent]`

> 각 봇의 자세한 명령어 사용법은 해당 봇 가이드 참조.

### 참석 통계 (`!attendance`)

보스 + 전투 참석 횟수를 자동으로 집계하여 순위를 보여줍니다.

**명령어:**
```
!attendance [기간]
!참석통계 [기간]
!랭킹 [기간]
```

**기간 옵션:**
- (없음) 또는 `all`: 전체 기간
- `today`: 오늘
- `week`: 최근 7일
- `month`: 이번 달
- `2026-06`: 특정 월 (YYYY-MM)
- `2026-06-01~2026-06-15`: 기간 지정 (YYYY-MM-DD~YYYY-MM-DD)

**출력:**
- Discord Embed: 페이지네이션 (30명/페이지)
  - 순위 + 이름 + 총 참석 횟수
  - 보스 참석 / 전투 참석 분리 표시
  - 1~3위 메달 표시 (🥇🥈🥉)
  - 버튼으로 페이지 이동 (◀ 이전 | 다음 ▶)
  - 30분 타임아웃 (버튼 클릭 시 자동 연장)
- Excel 파일: 전체 순위 데이터 (순위, 이름, Discord ID, 보스, 전투, 합계)

**예시:**
```
!attendance           # 전체 기간 통계
!attendance month     # 이번 달 통계
!attendance week      # 최근 7일 통계
!attendance 2026-06   # 2026년 6월 통계
!attendance 2026-06-01~2026-06-15  # 특정 기간
```

**집계 방식:**
- `Data/LinC_Boss_Data.xlsx`에서 보스 참석 횟수 집계
- `Data/LinC_Combat_Data.xlsx`에서 전투 참석 횟수 집계
- Discord ID 기준으로 두 데이터 합산
- 총 참석 횟수 내림차순 정렬

---

### 멤버 설문 (`!survey`)

파티 구성용 멤버 프로필(직업/레벨/강화/요정속성/특수스킬)을 입력받습니다. `Data/member_profiles.json`에 저장되어 파티 자동 편성에 사용됩니다.

| 명령어 | 동작 |
|--------|------|
| `!survey all` | 서버 전체 멤버에게 DM으로 설문 발송 |
| `!survey @멤버` | 해당 멤버에게 DM으로 설문 발송 (멤버 본인이 입력) |
| `!survey @멤버 here` | **관리자가 현재 채널에서 그 멤버의 정보를 대신 입력** (DM 아님) |

- 단계별 버튼/드롭다운 UI(클래스 → 레벨 → 무기/방어구 강화 → 요정속성·특수스킬 → 확인)
- 같은 멤버에게 다시 발송하면 **이전 설문 메시지는 자동 종료**되고 새 메시지가 생성됨 (DM·here 모두)
- 영속 View로 봇 재시작 후에도 진행 중인 설문 복구

---

### 참석 정산 (`/attpay`)

참석 횟수 기준으로 판매금을 분배하는 정산입니다. 슬래시 명령 `/attpay [금액] [기간]`로 호출 (비율 입력 팝업이 바로 열림). `!attpay`는 슬래시 사용 안내만 표시.

**기간(period) 형식:**
- (없음) / `all` : 전체 기간
- `today` / `week` / `month`
- `2026-06` : 특정 월
- `2026-06-01~2026-06-21` : 범위
- `~2026-06-21` : **처음부터** 그 날짜까지
- `2026-06-01~` : 그 날짜부터 **오늘까지**

**흐름:**
1. 비율 입력 팝업 (예: `혈비 10, 운영비 30, 분배금 30, 기본급 30`) — `accountant_config`의 `settlement` 값이 기본으로 채워짐
2. **분배 대상자 선택** 화면: 체크박스로 인원 제외/포함. 제외하면 그 인원의 참석 횟수가 총 참석 횟수에서도 빠져 1인당 분배가 재계산됨
3. "확정" 시 계산 + `RaidBook_Attendance_Distribution_Data.xlsx` 저장 + 결과 임베드/파일 게시

**추가 항목(기본급 등) n빵:** `settlement.extra_pcts`에 등록한 항목은 참석 횟수와 무관하게 **분배대상자 전체에게 동일 금액**으로 나눠 각자 분배금에 합산됩니다.

> `/attpay`는 `admin_channel_id` 외에 `allowed_attpay_channel_ids`로 허용된 채널에서도 사용할 수 있습니다.

---

## 🔗 IPC 통신 메커니즘

Watchdog은 각 봇의 IPC 서버에 TCP로 연결해 명령을 전달합니다.

### 포트 설정
각 봇의 config의 `ipc_port`:
- Boss Bot: `7701`
- Combat Bot: `7702`
- Accountant Bot: `7703`
- TTS Bot: IPC 없음 (독립)

### 전송 형식
```json
요청: {"cmd": "stats", "args": ["30", "25"]}
응답: {"ok": true, "messages": [...]}
응답(오류): {"ok": false, "error": "..."}
```

### Watchdog → 회계봇 정산 호출 예시
```
사용자: !account settle 2605221300
  ↓
Watchdog: _send_ipc("account", "_create_settle_panel", ["2605221300", 채널ID])
  ↓
회계봇 IPC: 보스 정보 조회 + 정산 패널 채널 게시
  ↓
Watchdog: 게시 완료 상태 메시지 표시
  ↓
사용자가 Accountant Bot이 게시한 "판매금 입력/분배대상자 변경/분리 정산" 버튼 클릭
  ↓
Accountant Bot: Modal/View 처리 → Excel 저장 + 정산 채널 알림 송신
```

### 트러블슈팅
- 봇이 죽었거나 IPC 포트가 막힌 경우: `⏳ 처리 중...` → 5~15초 후 타임아웃 메시지
- IPC 인자가 항상 **문자열**로 전달됨 (`int` 타입 힌트 우회) → 각 봇 명령에서 명시적 변환 처리 권장

---

## 📊 봇 상태 표시

| 아이콘 | 상태 |
|--------|------|
| 🟢 실행중 (PID: XXXXX) | 정상 실행 중 |
| 🟡 재시작 대기 | 봇이 종료되어 재시작 대기 중 |
| 🔴 중지 | 수동 중지 또는 빠른 재시작 제한 초과 |

---

## 🔄 자동 재시작 로직

1. 봇 프로세스가 종료되면 **`restart_delay_seconds`초 후 자동 재시작**
2. **`rapid_restart_window_seconds`초 내에 `max_rapid_restarts`회 이상** 재시작되면 해당 봇 감시 중단 (🔴 중지 상태)
3. 중단된 봇은 `start [봇이름]`으로 수동 재시작 가능

> 기본 설정: 60초 내 5회 이상 재시작 시 감시 중단

---

## 🌐 네트워크 체크

Watchdog은 별도 스레드에서 Discord Gateway/API TCP 연결을 주기적으로 확인합니다.

- 기본 체크 대상: `gateway.discord.gg:443`, `discord.com:443`
- 기본 주기: 60초
- 기본 타임아웃: 5초
- 기본 연결 시도: 대상별 최대 2개 IP
- 상태가 바뀔 때만 로그에 기록해서 불필요한 로그 반복을 줄입니다.
- `netcheck` 명령어로 즉시 수동 확인할 수 있습니다.

`discord.client Attempting a reconnect` 와 함께 `gateway.discord.gg:443 연결 실패`가 기록되면 봇 코드보다 방화벽, VPN, 프록시, 공유기, ISP/망 경로 문제를 먼저 확인하세요.

---

## 📁 폴더 구조

```
RaidBook_Bot/
├── RaidBook_Watchdog.py
├── RaidBook_Accountant_Bot.py
├── RaidBook_views_account.py
├── RaidBook_ipc.py
├── RaidBook_boss_Bot.py
├── RaidBook_Combat_Bot.py
├── RaidBook_TTS_Bot.py
├── Config/
│   ├── watchdog_config.json
│   ├── accountant_config.json
│   ├── boss_config.json
│   ├── combat_config.json
│   └── tts_config.json
├── Data/
│   ├── Backup/
│   ├── RaidBook_Accountant_Data.xlsx
│   ├── RaidBook_Distribution_Data.xlsx      # 분배/정산
│   ├── RaidBook_Boss_Data.xlsx
│   ├── RaidBook_Combat_Data.xlsx
│   ├── user_mapping.json                # UID ↔ 닉네임 (3개 봇 공유)
│   ├── uid_alias.json                   # 부계정/동명이인 통합 (3개 봇 공유)
│   ├── active_messages.json             # 보스 활성 메시지
│   ├── active_combat_messages.json      # 전투 활성 메시지
│   └── settlement_notifications.json    # 정산ID ↔ 알림 메시지ID
├── Log/
│   ├── RaidBook_Watchdog_YYYY-MM-DD.log
│   ├── RaidBook_Accountant_Bot_YYYY-MM-DD.log
│   ├── RaidBook_boss_Bot_YYYY-MM-DD.log
│   └── RaidBook_Combat_Bot_YYYY-MM-DD.log
└── Guide/
    ├── RaidBook_Accountant_Bot_가이드.md
    ├── RaidBook_boss_Bot_가이드.md
    ├── RaidBook_Combat_Bot_가이드.md
    ├── RaidBook_TTS_Bot_가이드.md
    └── RaidBook_Watchdog_가이드.md
```

---

## 📝 로그

- CMD 출력 + 로그 파일 동시 기록
- 로그 파일: `Log/RaidBook_Watchdog_YYYY-MM-DD.log`
- 실행 중 날짜가 바뀌어도 새 날짜 로그 파일로 자동 전환
- 봇 시작/종료/재시작/오류/명령어 실행 로그 기록
- Discord Gateway/API TCP 연결 상태를 주기적으로 기록
- 종료 코드는 16진수와 원인 추정을 함께 기록
- `0xC000013A` 는 Windows 콘솔 종료, `Ctrl+C`, 로그오프/세션 종료 계열 신호입니다. 여러 봇이 같은 시각에 이 코드로 종료되면 개별 봇 오류보다 Watchdog 콘솔/Windows 세션 종료 가능성이 큽니다.
- `Ctrl+C` 로 즉시 종료 (모든 봇 함께 종료)

---

## ⚠️ 주의사항

- Watchdog 종료(`quit` / `Ctrl+C`) 시 **모든 봇이 함께 종료**됩니다
- 각 봇의 Config 파일이 없으면 봇 시작 실패 — Config 먼저 설정 필수
- 로그 파일은 일자별로 자동 생성됩니다
- **Watchdog 이 띄운 봇 인스턴스 외에 같은 봇을 수동으로 띄우지 마세요.** 같은 디스코드 토큰으로 두 번 로그인되어 모든 명령이 중복 처리되고, 동일 엑셀 파일 동시 쓰기로 **데이터 손상**이 발생할 수 있습니다 (2026-05-17 회계봇 사고 사례). 현재 실행 중인 봇 확인:
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*RaidBook_*Bot*" } |
    ForEach-Object { "PID=$($_.ProcessId) $($_.CommandLine -split '\s+' | Select-Object -Last 1)" }
  ```
