@echo off
rem Build CCS3000.exe (double-click this file). Output: dist\CCS3000\CCS3000.exe
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3 was not found. Install it from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during setup.
    goto :fail
)

echo [1/3] Installing libraries...
%PY% -m pip install --upgrade pip
if errorlevel 1 goto :fail
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo [2/3] Building exe (takes a few minutes)...
%PY% -m PyInstaller --noconfirm --clean CCS3000.spec
if errorlevel 1 goto :fail

echo [3/3] Done.
echo.
echo   dist\CCS3000\CCS3000.exe
echo   (copy the whole dist\CCS3000 folder to the target PC)
echo.
if exist "dist\CCS3000" start "" explorer "dist\CCS3000"
pause
exit /b 0

:fail
echo.
echo [FAILED] Build did not finish - see the messages above.
pause
exit /b 1
