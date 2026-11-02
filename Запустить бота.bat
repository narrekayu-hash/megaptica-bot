@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Бот-модератор

if not exist ".env" (
    echo Не найден файл .env рядом с этим файлом. Скопируй .env.example в .env и впиши токен.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Первый запуск: готовлю всё нужное, это займёт минуту-две...
    py -3 -m venv venv
    if errorlevel 1 (
        echo Не получилось найти Python. Установи его с python.org и запусти этот файл снова.
        pause
        exit /b 1
    )
)

echo Проверяю, всё ли установлено...
venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Не получилось установить нужные библиотеки. Проверь интернет и запусти снова.
    pause
    exit /b 1
)

echo.
echo Бот запускается. Пока это окно открыто, бот работает.
echo Чтобы остановить, просто закрой окно.
echo.

:run
venv\Scripts\python -m bot
if errorlevel 3 if not errorlevel 4 (
    echo.
    echo В файле .env ошибка, текст выше. Исправь его и запусти этот файл снова.
    pause
    exit /b 1
)
echo.
echo Бот остановился. Через 10 секунд он запустится снова.
echo Если это повторяется, скопируй ошибку выше и пришли.
timeout /t 10 /nobreak >nul
goto run
