@echo off
REM ============================================
REM MindFinder 봇 로컬 실행 스크립트 (Windows)
REM 최초 실행 시 가상환경 생성 + 의존성 설치를 수행합니다.
REM ============================================
setlocal

cd /d "%~dp0"

REM 1) 가상환경이 없으면 생성
if not exist ".venv\Scripts\python.exe" (
    echo [MindFinder] 가상환경을 생성합니다...
    python -m venv .venv
    if errorlevel 1 (
        echo [오류] 가상환경 생성에 실패했습니다. Python 3.10+ 설치를 확인하세요.
        exit /b 1
    )
    echo [MindFinder] 의존성을 설치합니다...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM 2) .env 파일 확인
if not exist ".env" (
    echo [오류] .env 파일이 없습니다.
    echo        .env.example 을 복사해 .env 를 만들고 DISCORD_TOKEN 을 채워주세요.
    exit /b 1
)

REM 3) 봇 실행
echo [MindFinder] 봇을 시작합니다...
".venv\Scripts\python.exe" -m bot.main

endlocal
