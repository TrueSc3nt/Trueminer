# CryptoCrackersMiner

**BTC + BCH Dual Solo Miner with CUDA GPU Support**

Mine Bitcoin and Bitcoin Cash simultaneously on solo mining pools using both CPU and GPU. Features adaptive GPU difficulty, dual pool support, and Telegram notifications.

---

## Features

- **Dual Mining** — Mine BTC and BCH at the same time on separate solo pools
- **GPU Acceleration** — CUDA-powered SHA-256d mining at 500+ MH/s
- **Adaptive Difficulty** — GPU automatically adjusts difficulty based on share rate
- **Dual Pool Support** — Connect to 2 pools per coin for redundancy
- **Telegram Alerts** — Real-time notifications for shares and blocks
- **CPU + GPU** — Run CPU workers alongside GPU for maximum hashrate
- **Auto-tuning** — Automatic GPU block/grid optimization

## Quick Start

### Requirements
- Python 3.8+
- NVIDIA GPU with CUDA support (RTX 20xx or newer)
- CUDA Toolkit 13.2+ (for GPU mining)

### Install
```bash
git clone https://github.com/TrueSc3nt/cryptocrackersminer.git
cd cryptocrackersminer
pip install -r requirements.txt
```

### Run
```bash
# Interactive setup (recommended)
python miner.py

# CLI with all options
python miner.py --gpu --bch --bch-addr YOUR_BCH_ADDRESS \
  --tg-token YOUR_BOT_TOKEN --tg-chat YOUR_CHAT_ID --no-prompt

# Or use the batch file (Windows)
run.bat
```

## Mining Modes

| Mode | Command | Description |
|------|---------|-------------|
| BTC GPU | `--gpu` | BTC only with GPU acceleration |
| BTC CPU | (default) | BTC only with CPU cores |
| BCH GPU | `--gpu --bch` | BTC + BCH with GPU |
| BCH CPU | `--bch` | BTC + BCH with CPU |
| Both | `--gpu --bch` | Full CPU + GPU dual mining |
| GPU Only | `--gpu --gpu-only` | Skip CPU workers |

## GPU Difficulty

| Setting | Command | Behavior |
|---------|---------|----------|
| Auto | `--gpu-diff 0` | Starts easy, adapts to share rate |
| Fixed 1 | `--gpu-diff 1` | Easiest — finds shares fast |
| Fixed 4 | `--gpu-diff 4` | Balanced |
| Fixed 8 | `--gpu-diff 8` | Harder — fewer shares |

**Auto mode:** Starts at difficulty 1, doubles when shares come in, halves when silent for 1 hour.

## Telegram Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token (format: `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`)
3. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot)
4. Enter both when prompted during setup, or use CLI:
```bash
python miner.py --tg-token "YOUR_BOT_TOKEN" --tg-chat "YOUR_CHAT_ID"
```

### Telegram Notifications
- **GPU Share Found** — When GPU finds a valid share
- **Block Found** — When a block is solved
- **Share Accepted** — When pool accepts a share
- **Startup** — Miner configuration summary

## CLI Options

```
--gpu              Enable CUDA GPU mining
--gpu-only         GPU only, skip CPU workers
--gpu-diff N       GPU difficulty (0=auto, 1-10000=fixed)
--bch              Enable BCH dual mining
--bch-addr ADDR    Bitcoin Cash address
--bch-pool H:P     BCH pool (default: solo.bchpool.org:3333)
--pool1 H:P        BTC pool 1 (default: solo.ckpool.org:3333)
--pool2 H:P        BTC pool 2 (default: solo.stratum.braiins.com:3333)
--tg-token TOKEN   Telegram bot token
--tg-chat ID       Telegram chat ID
--no-prompt        Skip interactive setup
```

## Supported Pools

### BTC
| Pool | Host | Port |
|------|------|------|
| ckpool | solo.ckpool.org | 3333 |
| braiins | solo.stratum.braiins.com | 3333 |
| publicpool | public-pool.io | 3333 |
| minerpool | solo.minerpool.com | 3333 |

### BCH
| Pool | Host | Port |
|------|------|------|
| bchpool | solo.bchpool.org | 3333 |
| bchpublic | public-pool.bch.ninja | 3333 |
| bmcpool | solo.bmcpool.org | 3333 |

## Project Structure

```
cryptocrackersminer/
├── miner.py              # Main miner (BTC + BCH, CPU + GPU)
├── gpu_miner.py          # CUDA GPU wrapper
├── sha256_cuda.cu        # CUDA kernel source
├── sha256_cuda.h         # C API header
├── requirements.txt      # Python dependencies
├── run.bat               # Windows launcher (5 modes)
├── run_cpu_only.bat      # CPU-only launcher
├── README.md             # This file
└── .gitignore
```

## Building from Source

To rebuild the CUDA kernel:
```bash
# Requires CUDA Toolkit 13.2+ and Visual Studio
build_gpu.bat
```

Or manually:
```bash
nvcc --shared -o sha256_cuda.dll sha256_cuda.cu -Xcompiler "/MD /O2" -O3 --use_fast_math -arch=sm_86
```

## How It Works

1. **Connect** to solo mining pools via Stratum protocol
2. **Receive** block templates with merkle root, prevhash, nbits
3. **Compute** midstate (first 64 bytes of 80-byte header)
4. **Scan** nonces on GPU/CPU using double-SHA256d
5. **Compare** hash against pool difficulty target
6. **Submit** valid shares to pool
7. **Notify** via Telegram when shares are found

## Disclaimer

Solo mining is a lottery. The probability of finding a block is extremely low for individual miners. This software is provided for educational purposes.

## License

MIT
