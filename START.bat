@echo off
title MapleFlipper
cd /d "%~dp0"
set "APP=%LOCALAPPDATA%\MapleFlipper\app"
set "TOKF=%LOCALAPPDATA%\MapleFlipper\token.txt"
if not exist "%LOCALAPPDATA%\MapleFlipper" mkdir "%LOCALAPPDATA%\MapleFlipper"
rem Shared ZIP (code next to this file, no token): just run it, no update questions
if not exist "%TOKF%" if exist "%~dp0maple_flipper.py" goto local
:asktoken
set "TOK="
if not exist "%TOKF%" (
  echo.
  echo  One-time setup: paste your GitHub token and press Enter.
  echo  Make it at github.com/settings/personal-access-tokens/new
  echo    Repository access: Only select repositories - MapleFlipper
  echo    Permissions: Contents - Read-only
  set /p "TOK=Token: "
  call echo %%TOK%%> "%TOKF%"
)
echo Checking for updates...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $tok=(Get-Content $env:TOKF -Raw).Trim(); $z=\"$env:TEMP\mf.zip\"; $t=\"$env:TEMP\mf_new\"; Invoke-WebRequest 'https://api.github.com/repos/Mpixy/MapleFlipper/zipball/main' -Headers @{Authorization=\"Bearer $tok\"} -OutFile $z -UseBasicParsing; if (Test-Path $t) {Remove-Item $t -Recurse -Force}; Expand-Archive $z $t -Force; $src=(Get-ChildItem $t -Directory)[0].FullName; New-Item -ItemType Directory -Force $env:APP | Out-Null; Copy-Item \"$src\*\" $env:APP -Recurse -Force; Write-Host 'Up to date.' } catch { if ($_.Exception.Response -and @(401,403,404) -contains [int]$_.Exception.Response.StatusCode) { Remove-Item $env:TOKF -Force; Write-Host ''; Write-Host 'This token cannot open the MapleFlipper repo. Let''s try another one.'; exit 7 }; Write-Host ('Update check failed: ' + $_.Exception.Message); }"
if errorlevel 7 goto asktoken
if exist "%APP%\maple_flipper.py" cd /d "%APP%"
:local
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
"%VENV%\Scripts\python.exe" -m pip install -q rapidocr-onnxruntime rapidfuzz mss numpy requests || (echo Package install failed. & pause & exit /b)
echo ok> "%VENV%\ready"

:run
start "" "%VENV%\Scripts\pythonw.exe" "%CD%\maple_flipper.py"
