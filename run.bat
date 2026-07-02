@echo off
echo Installing dependencies...
pip install -q numpy requests psutil scikit-learn
echo.
echo ================================================================
echo   TrueMiner v1 - BTC + BCH Dual Mine
echo ================================================================
echo.
echo   [1] BTC only (GPU)
echo   [2] BTC only (CPU)
echo   [3] BTC + BCH (GPU only)
echo   [4] BTC + BCH (CPU only)
echo   [5] BTC + BCH (CPU + GPU)
echo.
set /p choice="  Select mode [1]: "
if "%choice%"=="" set choice=1

echo.
echo   GPU Difficulty:
echo     [0] Auto-adaptive (starts easy, adjusts automatically)
echo     [1] Fixed difficulty 1 (easiest)
echo     [4] Fixed difficulty 4
echo     [8] Fixed difficulty 8
set /p diff="  Select GPU difficulty [0]: "
if "%diff%"=="" set diff=0

echo.
set /p tg_use="  Enable Telegram notifications? (y/n) [n]: "
set TG_FLAG=
if /I "%tg_use%"=="y" (
    set /p tg_token="  Telegram Bot Token: "
    set /p tg_chat="  Telegram Chat ID: "
    if defined tg_token if defined tg_chat (
        set TG_FLAG=--tg-token !tg_token! --tg-chat !tg_chat!
    )
)

set GPU_FLAG=
if "%choice%"=="1" set GPU_FLAG=--gpu
if "%choice%"=="3" set GPU_FLAG=--gpu --gpu-only --bch
if "%choice%"=="5" set GPU_FLAG=--gpu --bch
if "%choice%"=="4" set GPU_FLAG=--bch

if not "%diff%"=="0" (
    set GPU_FLAG=%GPU_FLAG% --gpu-diff %diff%
)

if "%choice%"=="2" (
    echo Starting BTC mining with CPU only...
    python miner.py --no-prompt %TG_FLAG%
) else (
    python miner.py %GPU_FLAG% --no-prompt %TG_FLAG%
)
pause
