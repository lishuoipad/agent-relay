@echo off
if "%~1"=="" (
  echo Usage: open-status.cmd project.local.json [python.exe]
  exit /b 2
)
if "%~2"=="" (
  start "" pwsh.exe -NoProfile -STA -WindowStyle Hidden -File "%~dp0Android-Dev-Status.ps1" -ConfigPath "%~f1"
) else (
  start "" pwsh.exe -NoProfile -STA -WindowStyle Hidden -File "%~dp0Android-Dev-Status.ps1" -ConfigPath "%~f1" -PythonPath "%~f2"
)
