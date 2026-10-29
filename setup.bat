@echo off
cd /d "%~dp0"
title 한국어 AI 윤문 에이전트 - 설치
echo ============================================
echo   한국어 AI 윤문 에이전트 - 처음 한 번 설치
echo ============================================
echo.
echo [1/4] Python 확인 · 프로그램 준비
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || (
  echo.
  echo  Python 3.10 이상이 필요합니다.
  echo  https://www.python.org/downloads/ 에서 설치하세요.
  echo  설치 첫 화면 아래의 "Add python.exe to PATH" 를 꼭 체크하세요.
  echo  설치가 끝나면 이 창을 닫고 setup.bat 을 다시 더블클릭하세요.
  start "" https://www.python.org/downloads/
  pause & exit /b 1
)
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt || (echo pip 설치 실패 - 인터넷 연결을 확인하고 다시 실행하세요. & pause & exit /b 1)
echo       완료
echo.
echo [2/4] Codex CLI 확인 (ChatGPT 계정으로 AI를 부르는 도구)
set "PATH=%APPDATA%\npm;%PATH%"
where codex >nul 2>nul || (
  where npm >nul 2>nul || (
    echo.
    echo  Node.js 가 필요합니다.
    echo  https://nodejs.org/ 에서 LTS 버전을 설치한 뒤 setup.bat 을 다시 더블클릭하세요.
    start "" https://nodejs.org/
    pause & exit /b 1
  )
  call npm install -g @openai/codex
)
where codex >nul 2>nul || (echo Codex CLI 설치 실패 - 창을 닫고 setup.bat 을 다시 실행해 보세요. & pause & exit /b 1)
echo       완료
echo.
echo [3/4] Codex 로그인 확인
call codex login status >nul 2>nul || (
  echo  브라우저가 열리면 ChatGPT 계정으로 로그인하세요.
  echo  "로그인 완료" 화면이 뜨면 이 창으로 돌아오면 됩니다.
  call codex login
)
call codex login status
echo.
echo [4/4] 작업 폴더 준비
if not exist workspace mkdir workspace
echo       완료
echo.
echo ============================================
echo   설치 끝. 이제 run.bat 을 더블클릭하세요.
echo   (브라우저에서 http://localhost:8767/ 가 열립니다)
echo ============================================
pause
