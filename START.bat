@echo off
title MapleFlipper
cd /d "%~dp0"
set "APP=%LOCALAPPDATA%\MapleFlipper\app"
echo Checking for updates...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $z=\"$env:TEMP\mf.zip\"; $t=\"$env:TEMP\mf_new\"; Invoke-WebRequest 'https://github.com/Mpixy/MapleFlipper/archive/refs/heads/main.zip' -OutFile $z -UseBasicParsing; if (Test-Path $t) {Remove-Item $t -Recurse -Force}; Expand-Archive $z $t -Force; New-Item -ItemType Directory -Force $env:APP | Out-Null; Copy-Item \"$t\MapleFlipper-main\*\" $env:APP -Recurse -Force; Write-Host 'Up to date.' } catch { Write-Host 'Update check failed, using current version.' }"
if exist "%APP%\maple_flipper.py" cd /d "%APP%"
if not exist "maple_flipper.py" (
  echo.
  echo  Could not download MapleFlipper. Check your internet and run START.bat again.
  echo.
  pause
  exit /b
)
set "VENV=%LOCALAPPDATA%\MapleFlipper\venv"
if exist "%VENV%\ready" goto run

echo First run: setting things up (only once, a few minutes)...
set PY=
py -3.12 -V >nul 2>&1 && set "PY=py"
if not defined PY (
  echo Installing Python 3.12...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
)
if "%PY%"=="py" (py -3.12 -m venv "%VENV%") else ("%PY%" -m venv "%VENV%")
if not exist "%VENV%\Scripts\python.exe" (echo Python install failed. Install Python 3.12 from python.org and run again. & pause & exit /b)
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip -q
"%VENV%\Scripts\python.exe" -m pip install -q rapidocr-onnxruntime rapidfuzz mss numpy requests pynput || (echo Package install failed. & pause & exit /b)
echo ok> "%VENV%\ready"

:run
start "" "%VENV%\Scripts\pythonw.exe" "%CD%\maple_flipper.py"
