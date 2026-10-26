@echo off
rem Double-click launcher for Krishna's Super Bot.
rem Uses pythonw so no console window sits behind the app; falls back to python
rem (and keeps the window open) if something goes wrong, so errors stay readable.

cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "main.py"
    exit /b 0
)

python "main.py"
if %errorlevel% neq 0 (
    echo.
    echo Krishna's Super Bot exited with an error ^(code %errorlevel%^).
    echo If it mentions PySide6, run:  pip install -r requirements.txt
    echo.
    pause
)
