#!/usr/bin/env bash
set -euo pipefail

# ============================================
# MindFinder - 배포 서버 초기 세팅 (Debian/Ubuntu)
# - Tailscale 조인 + Docker 설치 + 저장소 clone + 최초 기동
# - root 로 실행:  sudo bash scripts/setup-server.sh
# ============================================

echo "=== MindFinder 서버 세팅 ==="
echo ""

# 1) 시스템 패키지
apt-get update -qq && apt-get install -y -qq curl git
echo "[OK] 시스템 패키지"

# 2) Tailscale 설치 + tailnet 조인  (CI 가 이 사설 IP 로 SSH 접속한다)
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "[안내] 아래 명령으로 tailnet 에 조인하세요 (브라우저 인증):"
  echo "        sudo tailscale up --ssh --hostname mindfinder-server"
  echo "      (--ssh 를 주면 별도 SSH 키 설정 없이 Tailscale SSH 로 접속 가능)"
fi
echo "[OK] tailscale"

# 3) Docker 설치
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
echo "[OK] docker"

# 4) 배포 계정
id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
echo "[OK] deploy 계정"

# 5) 저장소 clone
if [ -d /opt/mindfinder/.git ]; then
  sudo -u deploy git -C /opt/mindfinder pull
else
  mkdir -p /opt/mindfinder
  chown deploy:deploy /opt/mindfinder
  sudo -u deploy git clone https://github.com/Project-MindFinder/mindfinder.git /opt/mindfinder
fi
echo "[OK] 저장소 clone"

# 6) 최초 기동 (.env 가 있어야 함)
if [ -f /opt/mindfinder/.env ]; then
  cd /opt/mindfinder && docker compose up -d --build
  echo "[OK] 봇 기동"
  docker compose ps
else
  echo ""
  echo "[WARN] .env 파일이 없습니다. 아래를 실행하세요:"
  echo "  cd /opt/mindfinder"
  echo "  cp .env.example .env"
  echo "  nano .env    # DISCORD_TOKEN, GUILD_ID, RESET_VERIFICATIONS_ON_START=false"
  echo "  docker compose up -d --build"
fi

echo ""
echo "=== 세팅 완료 ==="
echo "이 서버의 Tailscale IP:"
tailscale ip -4 2>/dev/null || echo "  (tailscale up 후 'tailscale ip -4' 로 확인)"
echo ""
echo "위 IP 를 GitHub 저장소 Secret 'DEPLOY_HOST' 에 등록하세요."
echo "로그: cd /opt/mindfinder && docker compose logs -f"
