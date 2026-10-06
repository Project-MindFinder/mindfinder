# MindFinder 배포 가이드

로컬 테스트 후 VPS/서버에 Docker 로 배포하는 방법을 설명합니다.

## 1. 로컬 실행

### Windows
```bat
copy .env.example .env
REM .env 편집 (DISCORD_TOKEN 필수)
run.bat
```

### macOS / Linux
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 편집
python -m bot.main
```

## 2. 디스코드 봇 준비

1. [Discord Developer Portal](https://discord.com/developers/applications) 에서 애플리케이션 생성
2. **Bot** 탭에서 토큰 발급 → `.env` 의 `DISCORD_TOKEN` 에 입력
3. **Privileged Gateway Intents** 에서 다음을 활성화
   - `SERVER MEMBERS INTENT` (역할 부여/조회)
   - `MESSAGE CONTENT INTENT` (위기 키워드 감지)
4. **OAuth2 > URL Generator** 에서 `bot`, `applications.commands` 스코프 선택 후 서버에 초대

### 서버 사전 설정
- 인증 배지 역할 생성 → `VERIFIED_ROLE_ID`
- 심사자 역할 생성 → `ADMIN_ROLE_ID`
- 증빙 서류용 **비공개 채널** 생성 → `VERIFICATION_CHANNEL_ID`
  - 관리자 역할과 봇만 볼 수 있게 권한 설정
- (선택) 매칭 비공개 채널을 모아둘 **카테고리** 생성 → `MATCH_CATEGORY_ID`
  - 비워두면 서버 최상위에 채널이 생성됩니다.
- (선택) 개발용 서버 ID → `GUILD_ID` (슬래시 커맨드 즉시 반영)

### 봇 권한
매칭 시 비공개 채널을 생성/관리하므로 봇에 다음 권한이 필요합니다.
- **채널 관리(Manage Channels)** — 매칭 채널 생성 및 권한 설정
- **메시지 관리(Manage Messages)** — 심사 후 증빙 서류 삭제
- **역할 관리(Manage Roles)** — 인증 배지 부여 (봇 역할이 대상 역할보다 상위여야 함)

## 3. Docker 로 배포 (VPS)

### 사전 요구사항
- Docker 및 Docker Compose 설치

### 배포
```bash
# 서버에 소스 업로드 후
cd mindfinder
cp .env.example .env
nano .env    # 값 입력

docker compose up -d --build
```

### 운영 명령
```bash
docker compose logs -f          # 로그 확인
docker compose restart          # 재시작
docker compose down             # 중지
docker compose up -d --build    # 코드 변경 후 재배포
```

### 데이터 영속화
- `mindfinder-data` 볼륨 → SQLite DB (`/app/data`)
- `mindfinder-logs` 볼륨 → 로그 (`/app/logs`)

컨테이너를 재생성해도 데이터는 유지됩니다.

## 4. 백업

### Windows (로컬)
```bat
scripts\backup.bat
```

### Docker (VPS)
```bash
# 실행 중인 컨테이너에서 DB 파일 복사
docker compose cp mindfinder:/app/data/mindfinder.db ./backups/mindfinder_$(date +%Y%m%d_%H%M%S).db
```

정기 백업은 cron(리눅스) 또는 작업 스케줄러(윈도우)로 자동화하는 것을 권장합니다.

## 5. 업데이트 절차

```bash
git pull
docker compose up -d --build
```

스키마 변경이 있는 경우, SQLAlchemy `create_all` 은 기존 테이블을 변경하지 않으므로
필요 시 마이그레이션(Alembic 도입)을 검토하세요.

## 6. 문제 해결

| 증상 | 확인 사항 |
|---|---|
| 봇이 로그인하지 않음 | `DISCORD_TOKEN` 값, 네트워크 |
| 슬래시 커맨드가 안 보임 | `GUILD_ID` 설정 후 재시작, 봇 초대 스코프 |
| 역할 부여 실패 | 봇 역할이 대상 역할보다 상위인지 확인 |
| DM 알림 실패 | 이용자가 DM 을 차단한 경우 (로그에 기록됨). `/매칭요청함` 으로 확인 가능 |
| 서류 삭제 실패 | 봇에 `메시지 관리` 권한 필요 |
| 매칭 채널 생성 실패 | 봇에 `채널 관리` 권한이 있는지 확인. `MATCH_CATEGORY_ID` 가 올바른 카테고리인지 확인 |
| 매칭 채널이 안 보임 | 참여자에게 `채널 보기` 권한이 부여됐는지 확인 (`/매칭채널` 로 목록 확인) |
