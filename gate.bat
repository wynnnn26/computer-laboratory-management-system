@echo off
REM ============================================================
REM  Regression gate:  GUI suite -> integration suite -> layout
REM  sweep -> contrast sweep.  Run from the repo root.  Exits
REM  non-zero on the first stage that fails.
REM
REM    stage 1  _test_client_gui.py   (fresh DBs)
REM    stage 2  _test_integration.py  (fresh DBs + _ps_warm first)
REM    stage 3  _full_sweep.py        (clipped text / overflow)
REM    stage 4  _contrast_all.py      (unreadable-text contrast)
REM ============================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo === 1/4 client GUI suite ===
del /q lab_system.db* lab_client.db* 2>nul
python -u _test_client_gui.py > _gate_gui.log 2>&1
python -u _tail.py _gate_gui.log
findstr /c:"ALL CLIENT GUI TESTS PASSED" _gate_gui.log >nul
if errorlevel 1 goto :fail

echo.
echo === 2/4 integration suite ===
del /q lab_system.db* lab_client.db* 2>nul
python -u _ps_warm.py
if errorlevel 1 goto :fail
python -u _test_integration.py > _gate_int.log 2>&1
python -u _tail.py _gate_int.log
findstr /c:"ALL INTEGRATION TESTS PASSED" _gate_int.log >nul
if errorlevel 1 goto :fail

echo.
echo === 3/4 layout sweep ===
del /q lab_system.db* lab_client.db* 2>nul
python -u _full_sweep.py > _gate_sweep.log 2>&1
if errorlevel 1 (
    type _gate_sweep.log
    goto :fail
)
findstr /c:"NO LAYOUT PROBLEMS" _gate_sweep.log

echo.
echo === 4/4 contrast sweep ===
del /q lab_system.db* lab_client.db* 2>nul
python -u _contrast_all.py > _gate_contrast.log 2>&1
if errorlevel 1 (
    findstr /c:"below 3.0" _gate_contrast.log
    goto :fail
)
findstr /c:"below 3.0" _gate_contrast.log

echo.
echo ============ GATE PASSED ============
exit /b 0

:fail
echo.
echo ============ GATE FAILED ============
exit /b 1
