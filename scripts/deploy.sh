#!/usr/bin/env bash
# ============================================
# MindFinder 서버 배포 스크립트
# - 배포 서버에서 실행된다 (CI 가 SSH 로 원격 실행).
# - 최신 코드로 갱신한 뒤 Docker 로 재기동한다.
#
# 사용 예 (서버에서 직접):
#   DEPLOY_PATH=/opt/mindfinder DEPLOY_BRANCH=main bash scripts/deploy.sh
# ============================================
set -euo pipefail

# 배포 대상 경로 (미지정 시 현재 스크립트 기준 상위 디렉터리)
DEPLOY_PATH="${DEPLOY_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"

echo "[MindFinder] 배포 시작: path=${DEPLOY_PATH} branch=${DEPLOY_BRANCH}"

cd "${DEPLOY_PATH}"

# 1) 최신 코드로 갱신 (로컬 변경은 덮어씀 — 서버는 배포 전용)
git fetch --all --prune
git reset --hard "origin/${DEPLOY_BRANCH}"

# 2) .env 확인 (없으면 실패시켜 조용한 오작동 방지)
if [ ! -f .env ]; then
  echo "[MindFinder] 오류: .env 파일이 없습니다. 서버에서 먼저 생성하세요." >&2
  exit 1
fi

# 3) 이미지 빌드 및 재기동
docker compose build --pull
docker compose up -d
docker image prune -f

# 4) 상태 확인
docker compose ps

echo "[MindFinder] 배포 완료."
