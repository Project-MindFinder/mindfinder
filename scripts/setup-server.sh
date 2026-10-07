#!/usr/bin/env bash
set -euo pipefail

# ============================================
# MindFinder - 배포 서버 초기 세팅 (Debian/Ubuntu)
# - Docker 설치 + 저장소 clone + 최초 기동
# - 네트워크: 서버에는 Tailscale 을 설치하지 않는다.
#   라우터가 서브넷 라우터로 LAN 대역을 광고하고, CI 러너가 --accept-routes 로
#   서브넷 경로를 받아 서버의 LAN IP 로 SSH 접속한다.
# - root 로 실행:  sudo bash scripts/setup-server.sh
# ============================================

echo "=== MindFinder 서버 세팅 ==="
echo ""

# 1) 시스템 패키지
apt-get update -qq && apt-get install -y -qq curl git
echo "[OK] 시스템 패키지"

# 2) Docker 설치
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
echo "[OK] docker"

# 3) 배포 계정
id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
echo "[OK] deploy 계정"

# 4) 저장소 clone
if [ -d /opt/mindfinder/.git ]; then
  sudo -u deploy git -C /opt/mindfinder pull
else
  mkdir -p /opt/mindfinder
  chown deploy:deploy /opt/mindfinder
  sudo -u deploy git clone https://github.com/Project-MindFinder/mindfinder.git /opt/mindfinder
fi
echo "[OK] 저장소 clone"

# 5) 최초 기동 (.env 가 있어야 함)
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
echo "이 서버의 LAN IP (DEPLOY_HOST 에 등록할 값):"
echo "  ip -4 addr show | grep inet    # 예: 192.168.0.10"
echo ""
echo "네트워크: 라우터(Tailscale 서브넷 라우터)가 이 LAN 대역을 광고해야"
echo "          CI 러너가 --accept-routes 로 접속할 수 있습니다."
echo "로그: cd /opt/mindfinder && docker compose logs -f"
