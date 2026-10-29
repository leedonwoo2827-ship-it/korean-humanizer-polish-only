@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 한국어 AI 윤문 에이전트 - 이 창을 닫지 마세요
if not exist .venv\Scripts\python.exe (echo 먼저 setup.bat 을 더블클릭해 설치하세요. & pause & exit /b 1)
set "PATH=%APPDATA%\npm;%PATH%"
set PYTHONUTF8=1
echo 한국어 AI 윤문 에이전트 콘솔을 엽니다.
echo.
echo   주소: http://localhost:5294/
echo   브라우저가 안 열리면 위 주소를 주소창에 직접 입력하세요.
echo   (127.0.0.1 이 아니라 localhost 로 입력해야 합니다)
echo.
echo 이 검은 창은 끄지 말고 최소화해 두세요. 끄면 콘솔도 멈춥니다.
echo.
.venv\Scripts\python -m khum.server
pause
