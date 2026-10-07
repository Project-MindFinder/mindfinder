# MindFinder 실배포 & CI/CD 초기 세팅 가이드

> ⚠️ **필수 조건 4가지** (하나라도 빠지면 Deploy 가 실패한다)
> 1. OAuth 클라이언트에 **`tag:ci`** 부여 + tailnet ACL `tagOwners` 에 `tag:ci` 등록
>    → 없으면 `OAuth identity empty` 로 즉시 실패 (workflow 의 `tags: tag:ci` 는 고정값)
> 2. **라우터가 Tailscale 서브넷 라우터로 LAN 대역을 광고**하고, CI 노드(tag:ci)가
>    그 경로를 수용(`--accept-routes`, 워크플로에 포함) → 서버의 **LAN IP** 로 SSH
> 3. `DEPLOY_HOST` = 서버의 **LAN IP**(예: `192.168.0.10`) (서버에는 Tailscale 없음)
> 4. 서버 `/opt/mindfinder/.env` 에 `DISCORD_TOKEN`, `GUILD_ID`,
>    `RESET_VERIFICATIONS_ON_START=false` 존재

이 문서는 **GitHub 저장소 생성 → (서브넷 라우터 경유) 서버 접속 → CI/CD 자동배포**까지
처음부터 끝까지의 절차를 정리한 것입니다. 위에서 아래로 그대로 따라 하면 됩니다.

- 저장소: <https://github.com/Project-MindFinder/mindfinder>
- 배포 방식: GitHub Actions 러너가 **Tailscale** 로 tailnet 에 조인한 뒤,
  라우터(서브넷 라우터)가 광고하는 **LAN 대역 경로**를 통해
  서버의 **LAN IP** 로 **SSH** 접속하여 `docker compose` 로 재기동
- 로컬 PC는 배포 경로에 관여하지 않습니다. (서버 정보는 GitHub Secrets 로만 주입)

---

## 0. 구성 요약

```
[개발 PC] --git push--> [GitHub: Project-MindFinder/mindfinder]
                                 │ (push → Actions)
                                 ▼
                       [GitHub Actions 러너]
                  tailscale/github-action (tag:ci,
                  --accept-routes) 으로 tailnet 조인
                                 │  SSH (서버 LAN IP)
                                 ▼
                  [라우터 = Tailscale 서브넷 라우터]
                     LAN 대역(예: 192.168.0.0/24) 광고
                                 │
                                 ▼
                       [배포 서버 (LAN)]
                  docker compose up -d --build
```

- CI(`ci.yml`): push/PR 시 `pytest` 실행
- CD(`deploy.yml`): `main` push 시 서브넷 라우터 경유로 서버 배포
- 배포 스크립트: `scripts/deploy.sh` (서버에서 실행됨)

---

## 1. GitHub 저장소 준비

조직 `Project-MindFinder` 는 이미 생성되어 있습니다. 아래 명령은 로컬 저장소를
해당 조직에 올리는 절차입니다.

```bash
# (최초 1회) GitHub 로그인 — 브라우저 로그인창이 뜹니다
gh auth login --hostname github.com --git-protocol https --web \
  --scopes "repo,workflow,read:org"

# 조직에 비공개 저장소 생성 + 현재 폴더를 origin 으로 push
gh repo create Project-MindFinder/mindfinder \
  --private \
  --source=. \
  --remote=origin \
  --push
```

> 워크플로 파일(`.github/workflows/*`)을 push 하려면 토큰에 `workflow` 스코프가
> 필요합니다. 위 로그인 명령에 포함되어 있습니다.

이미 원격이 있거나 수동으로 추가하려면:

```bash
git remote add origin https://github.com/Project-MindFinder/mindfinder.git
git branch -M main
git push -u origin main
```

---

## 2. 배포 서버 준비 (1회)

배포 서버는 리눅스 계열(예: Debian/Ubuntu)을 기준으로 설명합니다.

> 이 서버에는 Tailscale 을 설치하지 않습니다. **라우터**가 Tailscale 서브넷 라우터로
> LAN 대역을 광고하고, CI 러너가 그 경로로 서버 LAN IP 에 접속합니다.

### 2-1. 라우터를 Tailscale 서브넷 라우터로 설정

라우터(Tailscale 설치된 장비)에서 LAN 대역을 광고합니다.

```bash
# 라우터에서 (예: LAN 이 192.168.0.0/24)
sudo tailscale up --advertise-routes=192.168.0.0/24 --accept-routes
```

그다음 Tailscale 관리 콘솔 → **Machines → 해당 라우터 → Edit route settings** 에서
광고된 서브넷(`192.168.0.0/24`)을 **승인(Approve)** 합니다.

ACL 의 `tagOwners` 에 CI 태그를 등록하고, tag:ci 노드가 서브넷 경로를 쓸 수 있게 합니다.

```jsonc
{
  "tagOwners": {
    "tag:ci": ["autogroup:admin"]
  }
}
```

> `tailscale ping` 은 서브넷 경로 대상에는 동작하지 않으므로 워크플로에서 사용하지 않습니다.

### 2-2. 서버 LAN IP 확인

```bash
# 배포 서버에서
ip -4 addr show | grep inet      # 예: 192.168.0.10  → DEPLOY_HOST 에 등록
```

### 2-3. Docker 설치

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # 재로그인 필요
docker --version && docker compose version
```

### 2-4. 배포 계정 및 디렉터리

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo usermod -aG docker deploy
sudo mkdir -p /opt/mindfinder && sudo chown deploy:deploy /opt/mindfinder

# 서버에 저장소 clone
sudo -u deploy git clone https://github.com/Project-MindFinder/mindfinder.git /opt/mindfinder
```

### 2-5. 서버 `.env` 생성 (비밀값은 서버에만)

```bash
cd /opt/mindfinder
cp .env.example .env
nano .env
```

운영 시 **반드시** 아래 값을 확인하세요.

| 변수 | 운영 권장값 | 설명 |
|---|---|---|
| `DISCORD_TOKEN` | (발급값) | 봇 토큰 |
| `RESET_VERIFICATIONS_ON_START` | **false** | true 면 재시작 시 인증데이터 초기화(테스트용) |
| `GUILD_ID` | (서버 ID) | 슬래시 커맨드 즉시 반영 |
| `ADMIN_ROLE_ID`, `VERIFIED_ROLE_ID` 등 | (역할 ID) | 역할 매핑 |
| `ENABLE_MEMBERS_INTENT` / `ENABLE_MESSAGE_CONTENT_INTENT` | true | 개발자 포털에서도 활성화 필요 |

### 2-6. 최초 기동

```bash
cd /opt/mindfinder
docker compose up -d --build
docker compose logs -f
```

---

## 3. Tailscale OAuth 클라이언트 생성 (CI 조인용)

GitHub Actions 러너가 tailnet 에 안전하게 조인하려면 OAuth 클라이언트가 필요합니다.

1. Tailscale 관리 콘솔 → **Settings → OAuth clients → Generate OAuth client**
2. **Scopes**: `Devices → Core` (쓰기) 선택
3. **Tags**: `tag:ci` 지정 (2-3 의 ACL 태그와 일치해야 함)
4. 발급된 **Client ID / Secret** 을 저장 (아래 4장 Secrets 에 사용)

---

## 4. GitHub Actions Secrets 등록

저장소 → **Settings → Secrets and variables → Actions → New repository secret**
아래 항목을 등록합니다. (노드 태그 `tag:ci` 는 워크플로에 **고정**되어 있어 시크릿이 필요 없습니다.)

| Secret | 필수 | 설명 / 예시 |
|---|---|---|
| `TS_OAUTH_CLIENT_ID` | ✅ | 3장에서 발급한 OAuth Client ID |
| `TS_OAUTH_SECRET` | ✅ | 3장에서 발급한 OAuth Client Secret |
| `DEPLOY_HOST` | ✅ | 서버 Tailscale IP 또는 MagicDNS 이름 (`100.x.y.z`) |
| `DEPLOY_USER` | ✅ | SSH 사용자 (`deploy`) |
| `DEPLOY_SSH_KEY` | ✅ | SSH 개인키 (아래 4-1 참고) |
| `DEPLOY_PATH` | ✅ | 서버 clone 경로 (`/opt/mindfinder`) |
| `DISCORD_TOKEN` | ✅ | 봇 토큰 (서버 `.env` 로 자동 작성) |
| `GUILD_ID` | ✅ | 디스코드 서버 ID |

### 4-1. SSH 키 생성 (로컬에서 1회)

```bash
ssh-keygen -t ed25519 -C "mindfinder-ci" -f mindfinder_ci -N ""

# 공개키 → 서버 배포 계정에 등록
ssh-copy-id -i mindfinder_ci.pub deploy@<DEPLOY_HOST>

# 개인키 전체 내용 → DEPLOY_SSH_KEY 시크릿에 붙여넣기
cat mindfinder_ci
```

> 서버에 Tailscale 이 없으므로 **SSH 키 방식**을 사용합니다.
> (`ssh-copy-id` 대상은 서버의 LAN IP)

---

## 5. 배포 실행 및 확인

- `main` 브랜치에 push 하면 **Deploy** 워크플로가 자동 실행됩니다.
- 수동 실행: **Actions → Deploy → Run workflow**
- 진행 로그: **Actions → Deploy → 최근 실행 → deploy 잡**

서버에서 직접 확인:

```bash
cd /opt/mindfinder
docker compose ps
docker compose logs -f --tail=100
```

---

## 6. 자주 겪는 문제

| 증상 | 원인 / 해결 |
|---|---|
| `OAuth identity empty` (즉시 실패) | OAuth 클라이언트에 `tag:ci` 권한 없음. ACL `tagOwners` 에 `tag:ci` 등록 + OAuth 클라이언트 Tags 에 `tag:ci` 지정 |
| `ssh: connect to host ... Connection timed out` | 라우터 서브넷 라우트 미승인/미광고. 라우터 `--advertise-routes` + 콘솔 **route 승인** + `DEPLOY_HOST` 가 **LAN IP** 인지 확인 |
| `Permission denied (publickey)` | `DEPLOY_SSH_KEY` 미등록 또는 서버 `authorized_keys` 누락 |
| `Host key verification failed` | `DEPLOY_HOST`(서버 LAN IP) 도달 가능 여부 확인 |
| `.env 파일이 없습니다` | 2-5 단계 미실행. 서버에 `.env` 생성 필요 |
| 재시작 때 인증데이터가 사라짐 | `.env` 의 `RESET_VERIFICATIONS_ON_START=false` 로 설정 |
| 봇이 로그인 못 함 | `DISCORD_TOKEN` 확인 + 개발자 포털 특권 인텐트 활성화 |

---

## 7. 로컬 개발 (참고)

```bash
# Windows
run.bat

# macOS / Linux
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

테스트:

```bash
pytest -q
```
