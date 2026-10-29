@echo off
cd /d "%~dp0"
set "PATH=%APPDATA%\npm;%PATH%"
echo Codex(ChatGPT) 로그인을 다시 합니다.
echo "Failed to refresh token / 401 / sign in again" 오류가 날 때 쓰세요.
echo.
call codex logout
call codex login
echo.
call codex login status
echo 끝나면 콘솔(http://localhost:8767/)에서 멈췄던 버튼을 다시 누르세요.
pause
