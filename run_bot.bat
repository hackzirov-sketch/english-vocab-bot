@echo off
chcp 65001 >nul
title English Vocabulary Master - Telegram Bot
cd /d "%~dp0"

if not exist ".venv\" (
    echo Creating virtual environment...
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Python not found! Please install Python 3.10+.
        pause
        exit /b 1
    )
)

echo Activating virtual environment...
call .venv\Scripts\activate.bat

echo Installing requirements...
pip install -q -r requirements.txt

if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env from .env.example...
        copy .env.example .env >nul
    )
)

echo Checking BOT_TOKEN...
findstr /B "BOT_TOKEN=" .env >nul
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] BOT_TOKEN is missing in .env
    echo Bot will not start without a valid BOT_TOKEN.
    echo Opening .env in Notepad for editing...
    start notepad .env
    pause
)
findstr /B "BOT_TOKEN=your_telegram_bot_token_here" .env >nul
if %ERRORLEVEL% EQU 0 (
    echo [WARNING] BOT_TOKEN is still set to the placeholder value.
    echo Opening .env in Notepad for editing...
    start notepad .env
    pause
)

echo.
echo Starting Telegram bot...
echo.
python bot.py

pause
