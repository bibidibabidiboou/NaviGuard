@echo off
REM Builds release\NaviGuard.exe (single portable file, no console window)
setlocal
cd /d "%~dp0"
set APPNAME=NaviGuard
if not exist .venv python -m venv .venv || goto :err
call .venv\Scripts\python.exe -m pip install --quiet --upgrade pyinstaller pillow pystray || goto :err
if not exist assets\logo.ico call .venv\Scripts\python.exe tools\make_icon.py || goto :err
if exist release rmdir /s /q release
.venv\Scripts\pyinstaller.exe -y --onefile --windowed --name %APPNAME% --hidden-import pystray._win32 ^
  --icon "%~dp0assets\logo.ico" ^
  --add-data "%~dp0assets\logo.ico;assets" --add-data "%~dp0assets\logo-64.png;assets" ^
  --distpath release --workpath build --specpath build --paths src src\gui.py || goto :err
copy /y README.md release\ >nul
copy /y blocklist.txt release\ >nul
copy /y allowlist.txt release\ >nul
echo.
echo Done: release\%APPNAME%.exe
exit /b 0
:err
echo Build failed.
exit /b 1
