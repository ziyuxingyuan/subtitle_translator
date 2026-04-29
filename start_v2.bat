@echo off
setlocal
if /i "%~1" NEQ "-hidden" (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%ComSpec%' -ArgumentList '/c','\"%~f0\" -hidden' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
  exit /b
)
cd /d "%~dp0"

rem Prefer local extracted config unless explicitly overridden.
if not defined SUBTITLE_TRANSLATOR_CONFIG_DIR set "SUBTITLE_TRANSLATOR_CONFIG_DIR=%~dp0data\config"
if not exist "%SUBTITLE_TRANSLATOR_CONFIG_DIR%" mkdir "%SUBTITLE_TRANSLATOR_CONFIG_DIR%" >nul 2>nul

set "PYW="
set "USE_PYW_LAUNCHER=0"

rem 1) Prefer local virtual environments
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
if not defined PYW if exist "venv\Scripts\pythonw.exe" set "PYW=venv\Scripts\pythonw.exe"
if not defined PYW if exist "env\Scripts\pythonw.exe" set "PYW=env\Scripts\pythonw.exe"

rem 2) Fallback to system pythonw on PATH
if not defined PYW (
  for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%I"
  )
)

rem 3) Last fallback to pyw launcher
if not defined PYW (
  for /f "delims=" %%I in ('where pyw.exe 2^>nul') do (
    if not defined PYW (
      set "PYW=%%I"
      set "USE_PYW_LAUNCHER=1"
    )
  )
)

if not defined PYW (
  echo [ERROR] No pythonw/pyw found.
  echo [HINT] Install Python or create ".venv" in this folder.
  exit /b 1
)

if "%USE_PYW_LAUNCHER%"=="1" (
  "%PYW%" -3 "%~dp0subtitle_translator_gui_v2.pyw"
) else (
  "%PYW%" "%~dp0subtitle_translator_gui_v2.pyw"
)
endlocal
