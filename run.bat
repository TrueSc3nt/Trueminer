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
echo   Wallet / payout address (REQUIRED for solo mining):
echo     Used as your pool username - solo pools send NO work without a valid address.
set /p btc_addr="  Your BTC address: "
set ADDR_ARG=
if defined btc_addr set ADDR_ARG="!btc_addr!"
if not defined btc_addr echo   WARNING: no address entered - the pool will likely send no work.

set BCH_ADDR_FLAG=
if "!BCH_MODE!"=="1" (
    set /p bch_addr="  Your BCH address (Enter to reuse BTC address): "
    if defined bch_addr set BCH_ADDR_FLAG=--bch-addr "!bch_addr!"
)

echo.
echo   Pool configuration:
echo     [Enter] = keep default pools (solo.ckpool.org:3333 + solo.stratum.braiins.com:3333)
echo     Or type your pool as host:port  (Stratum V2: stratum2+tcp://host:port/AUTHKEY)
set POOL_FLAG=
set BCH_POOL_FLAG=
set /p pool1="  BTC Pool #1 (primary) [Enter=default]: "
if defined pool1 (
    set POOL_FLAG=--pool1 "!pool1!"
    set /p pool2="  BTC Pool #2 (backup, optional): "
    if defined pool2 set POOL_FLAG=!POOL_FLAG! --pool2 "!pool2!"
)
if "!BCH_MODE!"=="1" (
    set /p bch_pool="  BCH Pool (host:port) [Enter=default]: "
    if defined bch_pool set BCH_POOL_FLAG=--bch-pool "!bch_pool!"
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
    python miner.py %ADDR_ARG% --no-prompt %POOL_FLAG% %BCH_POOL_FLAG% %BCH_ADDR_FLAG% %TG_FLAG%
) else (
    python miner.py %ADDR_ARG% %GPU_FLAG% --no-prompt %POOL_FLAG% %BCH_POOL_FLAG% %BCH_ADDR_FLAG% %TG_FLAG%
)
pause
