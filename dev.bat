@echo off
REM Data Removal CLI — Windows helper
REM Usage: dev.bat [command]

if "%1"=="" goto help
if "%1"=="setup" goto setup
if "%1"=="test" goto test
if "%1"=="lint" goto lint
if "%1"=="fmt" goto fmt
if "%1"=="run" goto run
if "%1"=="brokers" goto brokers
if "%1"=="browser" goto browser
if "%1"=="clean" goto clean
if "%1"=="reset-db" goto resetdb
goto help

:setup
echo [*] Creating virtual environment...
python -m venv .venv
call .venv\Scripts\activate
pip install --upgrade pip setuptools wheel -q
pip install -e ".[dev]" -q
echo [+] Setup complete. Run: .venv\Scripts\activate
goto end

:test
call .venv\Scripts\activate
python -m pytest tests/ -v
goto end

:lint
call .venv\Scripts\activate
pip install mypy ruff -q
ruff check dataremoval\ tests\
mypy dataremoval\ --ignore-missing-imports
goto end

:fmt
call .venv\Scripts\activate
pip install ruff -q
ruff format dataremoval\ tests\
ruff check --fix dataremoval\ tests\
goto end

:run
call .venv\Scripts\activate
dr --help
goto end

:brokers
call .venv\Scripts\activate
dr brokers list
goto end

:browser
call .venv\Scripts\activate
pip install playwright
python -m playwright install chromium --with-deps
goto end

:clean
if exist .venv rmdir /s /q .venv
if exist *.egg-info rmdir /s /q *.egg-info
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo [+] Cleaned
goto end

:resetdb
del /f "%LOCALAPPDATA%\data-removal\data-removal\data.db" 2>nul
echo [+] Database reset
goto end

:help
echo.
echo   Data Removal CLI — dev commands
echo.
echo   dev setup      Create venv and install
echo   dev test       Run tests
echo   dev lint       Run type check + linter
echo   dev fmt        Format code
echo   dev run        Show CLI help
echo   dev brokers    List supported brokers
echo   dev browser    Install Playwright browsers
echo   dev clean      Remove venv and build artifacts
echo   dev reset-db   Delete local database
echo.

:end
