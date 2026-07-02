@echo off
echo Installing dependencies...
pip install -r requirements.txt -q
echo.
echo Starting TrueCryptoMiner v10 (CPU only)...
python miner.py
pause
