을@echo off
REM ============================================
REM MindFinder SQLite DB 백업 스크립트 (Windows)
REM backups 폴더에 날짜/시간별 백업본을 생성합니다.
REM ============================================
setlocal

cd /d "%~dp0\.."

set DB_PATH=data\mindfinder.db
if not exist "%DB_PATH%" (
    echo [오류] DB 파일을 찾을 수 없습니다: %DB_PATH%
    exit /b 1
)

if not exist "backups" mkdir backups

REM 타임스탬프 생성 (YYYYMMDD_HHMMSS)
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set datetime=%%I
set STAMP=%datetime:~0,8%_%datetime:~8,6%

set BACKUP_FILE=backups\mindfinder_%STAMP%.db
copy "%DB_PATH%" "%BACKUP_FILE%" >nul

if errorlevel 1 (
    echo [오류] 백업에 실패했습니다.
    exit /b 1
)

echo [MindFinder] 백업 완료: %BACKUP_FILE%
endlocal
