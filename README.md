# MindFinder 🧭

폐쇄병동에 입원했던 경험이 있는 사람들이 서로를 찾고, 안전하게 연결될 수 있도록 돕는 **디스코드 봇**입니다.

## 핵심 기능

1. **입원 이력 인증** — 어떤 병원에 언제·얼마나 입원했는지 등록 (자기 신고 + 선택적 서류 심사)
2. **자동 진위확인** — 서울대병원 입퇴원 사실확인서 PDF 를 올리면 진위·정신과 여부를 자동 검증해 **즉시 인증** ([아래 참고](#서울대병원-입퇴원-사실확인서-자동-진위확인))
3. **동료 매칭** — 같은 시기·같은 병원에 입원했던 사람끼리 (매칭을 원하는 사람에 한해) **비공개 채널**에서 대화
4. **커뮤니티** — 전체 / 병원 / 기간 단위로 이야기를 나눌 수 있는 공간

> ⚠️ **개인정보 보호 원칙**: 봇은 전화번호/SNS를 **저장하거나 전달하지 않습니다**. 매칭이 성사되면 봇이 **비공개 채널**을 만들어 당사자들만 초대하며, 봇은 채널 접근 권한만 관리합니다.
>
> 🔒 **여러 명이 한 채널을 공유**할 수 있습니다. 같은 병원·시기 동료라면 `/매칭초대` 로 기존 비공개 채널에 추가로 초대할 수 있어 1:1 대화에 국한되지 않습니다.

## 기술 스택

| 항목 | 내용 |
|---|---|
| 언어 | Python 3.10+ |
| 프레임워크 | discord.py 2.4+ |
| ORM | SQLAlchemy 2.0 (async) |
| DB | SQLite (aiosqlite) |
| 배포 | 로컬 실행 → Docker / VPS |

## 시작하기 (로컬)

### 1. 요구사항
- Python 3.10 이상
- 디스코드 봇 토큰 ([Discord Developer Portal](https://discord.com/developers/applications))

### 2. 설치 및 실행 (Windows)

```bat
REM 프로젝트 루트에서
copy .env.example .env
REM .env 를 열어 DISCORD_TOKEN 등 값을 채우세요.

run.bat
```

`run.bat` 은 최초 실행 시 자동으로 가상환경 생성 + 의존성 설치 + 봇 실행을 수행합니다.

### 3. 수동 설치 (macOS/Linux)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 편집
python -m bot.main
```

## 환경변수 (.env)

| 변수 | 필수 | 설명 |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | 봇 토큰 |
| `GUILD_ID` | | 테스트 서버 ID (입력 시 슬래시 커맨드 즉시 반영) |
| `ADMIN_ROLE_ID` | | 심사자 역할 ID |
| `VERIFIED_ROLE_ID` | | 인증 배지 역할 ID |
| `VERIFICATION_CHANNEL_ID` | | 증빙 서류 제출 비공개 채널 ID |
| `CRISIS_CHANNEL_ID` | | 위기 안내 채널 ID (선택) |
| `MATCH_CATEGORY_ID` | | 매칭 비공개 채널을 만들 카테고리 ID (선택, 비우면 서버 최상위) |
| `SNUH_TRUTH_ENABLED` | | 서울대병원 자동 진위확인 사용 여부 (기본 `true`) |
| `SNUH_TRUTH_BASE_URL` | | 진위확인 사이트 주소 (기본 `https://sdoc.snuh.org`) |
| `SNUH_TRUTH_TIMEOUT` | | 진위확인 요청 타임아웃 초 (기본 `20`) |
| `DATABASE_PATH` | | SQLite 파일 경로 (기본 `data/mindfinder.db`) |
| `LOG_DIR` | | 로그 디렉터리 (기본 `logs`) |
| `LOG_LEVEL` | | 로그 레벨 (기본 `INFO`) |

## 주요 명령어

### 인증
- `/인증` — 병원 + 입원 연월 + 퇴원 연월 등록 (자기 신고)
- `/진위확인` — 서울대병원 입퇴원 사실확인서 PDF 자동 진위확인 + 자동 인증
- `/내이력` — 내가 등록한 이력 조회
- `/서류제출` — 서류 심사 요청 (인증 배지 신청)
- `/심사목록`, `/심사승인`, `/심사거절` — [관리자] 심사 처리

### 매칭
- `/매칭` — 매칭 희망 여부 설정 (on/off)
- `/매칭찾기` — 같은 병원·시기 동료 찾기
- `/매칭신청` — 후보에게 매칭 신청
- `/매칭요청함` — 받은 신청 확인
- `/매칭수락`, `/매칭거절` — 신청 처리 (수락 시 **비공개 채널 생성 + 초대**)
- `/매칭채널` — 내가 참여 중인 매칭 비공개 채널 목록
- `/매칭초대` — 같은 병원·시기 동료를 내 매칭 채널에 추가 초대 (여러 명 공유)
- `/매칭종료` — 매칭 채널 닫기 (기록은 보존)

> 매칭이 성사되면 봇이 참여자들만 볼 수 있는 **비공개 텍스트 채널**을 만들어 초대합니다. 연락처는 저장·전달하지 않으며, 채널 접근 권한만 관리합니다.

### 커뮤니티 · 기타
- `/커뮤니티` — 병원·기간 단위 스레드 열기
- `/병원목록` — 대표 병원 목록
- `/도움말` — 사용 안내
- `/도움` — 위기 상담 안내

## 서울대병원 입퇴원 사실확인서 자동 진위확인

서울대학교병원에서 발급한 **입퇴원 사실확인서** PDF 를 `/진위확인` 명령에 첨부하면,
다음 과정을 자동으로 수행한 뒤 **관리자 심사 없이 즉시 인증**합니다.

1. **PDF 파싱** — 문서번호, 발급번호, 진료과, 입퇴원 기간을 추출
2. **정신과 판정** — 진료과에 정신건강의학과(소아정신과 등)가 포함되는지 확인
3. **진위확인** — 서울대병원 문서 진위확인 API 로 문서번호를 조회해 **위조 여부** 검증
4. **자동 승인** — 위 조건을 모두 만족하면 인증 배지(🟢)를 즉시 부여하고 이력을 등록

> 문서 진위확인은 서울대병원 공식 사이트(`sdoc.snuh.org`)의 **문서확인번호** 조회를 이용합니다.
> 문서번호가 중복 등록되는 것을 막아 같은 서류로 중복 인증할 수 없습니다.

### 구현 메모
- [`bot/utils/certificate.py`](bot/utils/certificate.py) — PDF 텍스트 추출 및 사실확인서 파싱
- [`bot/utils/snuh_truth.py`](bot/utils/snuh_truth.py) — 진위확인 API 클라이언트 (CSRF/세션 처리)
- 사이트의 WAF 가 일반 Python HTTP 클라이언트를 차단(403)하므로 브라우저 TLS 지문을 흉내내는
  [`curl_cffi`](https://github.com/lexiforest/curl_cffi) 를 사용합니다.
- 자동 진위확인은 **서울대학교병원** 서류만 지원합니다. 그 외 병원은 `/서류제출` 로 관리자 심사를 이용합니다.

## 프로젝트 구조

```
mindfinder/
├── bot/
│   ├── main.py              # 진입점
│   ├── core.py              # 봇 클래스 (DB/코그/동기화)
│   ├── config.py            # 환경변수 설정
│   ├── logging_config.py    # 로깅
│   ├── db/
│   │   ├── base.py          # SQLAlchemy Base
│   │   ├── models.py        # ORM 모델
│   │   ├── database.py      # async 엔진/세션
│   │   ├── repositories.py  # 데이터 접근
│   │   └── seed.py          # 시드 실행
│   ├── cogs/                # 슬래시 커맨드
│   │   ├── general.py       # 도움말/병원목록
│   │   ├── verification.py  # 인증/심사
│   │   ├── matching.py      # 매칭 (후보/신청/비공개 채널)
│   │   ├── community.py     # 커뮤니티
│   │   └── crisis.py        # 위기 대응
│   ├── data/                # 시드 데이터
│   └── utils/               # 기간/병원 유틸
│       ├── periods.py       # 기간(연·월) 검증
│       ├── hospitals.py     # 병원명 정규화
│       ├── certificate.py   # 입퇴원 사실확인서 PDF 파싱
│       └── snuh_truth.py    # 서울대병원 진위확인 API
├── tests/                   # 단위 테스트
├── Dockerfile
├── docker-compose.yml
└── run.bat
```

## 테스트

```bash
pip install -e ".[dev]"
pytest
```

## Docker 배포 (VPS)

```bash
# .env 준비 후
docker compose up -d --build

# 로그 확인
docker compose logs -f
```

SQLite DB 와 로그는 named volume(`mindfinder-data`, `mindfinder-logs`)에 저장되어 컨테이너 재시작에도 유지됩니다.

### 백업

```bat
REM Windows
scripts\backup.bat
```

자세한 배포 방법은 [`docs/DEPLOY.md`](docs/DEPLOY.md) 를 참고하세요.

## 개인정보 및 안전

- 개인정보 처리방침: [`docs/PRIVACY.md`](docs/PRIVACY.md)
- 위기 상황 시 상담 안내: `/도움` 명령 또는 위험 키워드 자동 감지

## 라이선스

Proprietary (비공개)
