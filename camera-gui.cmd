@echo off
if not "%1"=="admin" (
    net session >nul 2>&1
    if %errorlevel% neq 0 (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" admin\"' -Verb RunAs" 2>nul
        if %errorlevel% equ 0 exit /b
    )
)
set "PYTHONPATH=%~dp0python_packages"
start "Camera IP Tools" "%~dp0runtime\pythonw.exe" "%~dp0camera_gui.py"
