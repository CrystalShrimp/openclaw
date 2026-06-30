@echo off
chcp 65001 >nul 2>&1
title OpenClaw

cd /d D:\ForRunning\ForDev\openclaw

if not exist .env (
    echo [ERROR] .env file not found. Copy .env.example to .env and fill in your config.
    echo   copy .env.example .env
    pause
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found. Run: uv sync
    pause
    exit /b 1
)

echo Starting OpenClaw...
.venv\Scripts\python.exe -m app.main

pause
