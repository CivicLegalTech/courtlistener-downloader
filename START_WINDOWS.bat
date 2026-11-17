@echo off
cd /d "%~dp0"
title CourtListener - 15 Second Auto Resume
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 download.py
) else (
    python download.py
)
echo.
echo Download session finished. The window remains open.
echo To start it again, run START_WINDOWS.bat.
cmd /k
