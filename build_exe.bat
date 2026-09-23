@echo off
REM ============================================================
REM  Build script for Computer Laboratory Management System
REM  (LAN Client-Server / Internet Cafe Edition)
REM  Run this on a Windows PC that has Python 3.9+ installed.
REM  Produces:
REM    dist\server.exe   - direct Server + Admin Console mode
REM    dist\client.exe   - direct Client kiosk mode
REM ============================================================

echo Checking Python installation...
python --version
if errorlevel 1 (
    echo Python was not found. Please install Python from https://www.python.org/downloads/
    echo IMPORTANT: check "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo Building server.exe (Server + Admin Console - opens directly in Server mode)...
python -m PyInstaller --noconfirm --onefile --windowed --name "server" main.py

echo Building client.exe (Client kiosk - opens directly in Client mode)...
python -m PyInstaller --noconfirm --onefile --windowed --name "client" main.py

echo.
echo ============================================================
echo  Build complete:
echo    dist\server.exe  - run on the Admin/Server PC
echo                        (starts directly in Server/Admin mode,
echo                         no Server/Client choice prompt)
echo    dist\client.exe  - run on every client/lab PC
REM (the mode is detected from the executable name "server"/"client")
echo.
echo  A lab_system.db file is created next to server.exe on first run.
echo  Client PCs ask for the Server IP once; it is saved next to
echo  client.exe and reused automatically (auto-reconnect included).
echo ============================================================
pause
