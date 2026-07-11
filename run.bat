@echo off
setlocal enabledelayedexpansion
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

set BCH_MODE=0
if "%choice%"=="3" set BCH_MODE=1
if "%choice%"=="4" set BCH_MODE=1
if "%choice%"=="5" set BCH_MODE=1

echo.
echo   GPU Difficulty:
echo     [0] Auto-adaptive (starts easy, adjusts automatically)
echo     [1] Fixed difficulty 1 (easiest)
echo     [4] Fixed difficulty 4
echo     [8] Fixed difficulty 8
set /p diff="  Select GPU difficulty [0]: "
if "%diff%"=="" set diff=0

echo.
echo   Pool configuration:
echo     [Enter] = keep default pools (solo.ckpool.org:3333 + solo.stratum.braiins.com:3333)
echo     Enter pools as host:port  (for Stratum V2 use stratum2+tcp://host:port/AUTHKEY)
set /p pool_custom="  Enter custom pools? (y/n) [n]: "
set POOL_FLAG=
set BCH_POOL_FLAG=
if /I "!pool_custom!"=="y" (
    echo.
    set /p pool1="  BTC Pool #1 (primary): "
    set /p pool2="  BTC Pool #2 (backup, optional): "
    if defined pool1 set POOL_FLAG=!POOL_FLAG! --pool1 "!pool1!"
    if defined pool2 set POOL_FLAG=!POOL_FLAG! --pool2 "!pool2!"
    if "!BCH_MODE!"=="1" (
        set /p bch_pool="  BCH Pool (host:port, optional): "
        if defined bch_pool set BCH_POOL_FLAG=--bch-pool "!bch_pool!"
    )
)

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
    python miner.py --no-prompt %POOL_FLAG% %BCH_POOL_FLAG% %TG_FLAG%
) else (
    python miner.py %GPU_FLAG% --no-prompt %POOL_FLAG% %BCH_POOL_FLAG% %TG_FLAG%
)
pause
