@echo off
REM ============================================================
REM  Build script for Computer Laboratory Management System
REM  (LAN Client-Server Edition)
REM  Run this on a Windows PC that has Python 3.9+ installed.
REM  Produces:
REM    dist\LabServer.exe   (server + admin console + launcher)
REM    dist\LabClient.exe   (fullscreen client kiosk for lab PCs)
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
echo Building LabServer.exe (Server + Admin Console)...
python -m PyInstaller --noconfirm --onefile --windowed --name "LabServer" main.py

echo Building LabClient.exe (Client kiosk for lab PCs)...
python -m PyInstaller --noconfirm --onefile --windowed --name "LabClient" client.py

echo.
echo ============================================================
echo  Build complete:
echo    dist\LabServer.exe  - run on the Admin/Server PC
echo    dist\LabClient.exe  - run on every client/lab PC
echo  A lab_system.db file is created next to LabServer.exe on
echo  first run. Client PCs ask for the Server IP on first run.
echo ============================================================
pause
