# ============================================
# MindFinder 봇 Dockerfile
# python:3.11-slim 기반의 경량 이미지
# ============================================
FROM python:3.11-slim

# 파이썬이 .pyc 를 만들지 않고, 출력을 버퍼링하지 않도록 설정
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

# 시스템 시간대 설정
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 복사/설치 (레이어 캐시 활용)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY bot ./bot
COPY pyproject.toml README.md ./

# 데이터/로그 디렉터리 (볼륨 마운트 지점)
RUN mkdir -p /app/data /app/logs

# 비루트 사용자로 실행 (보안 강화)
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# 봇 실행
CMD ["python", "-m", "bot.main"]
