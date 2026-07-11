#!/usr/bin/env python3
"""
TrueCryptoMiner v9 - by Truescent
Dual Pool + GPU Edition
bc1qke9ets26d6vs8ardndteds57frcald98n8g3te

v9 changes:
  + CUDA GPU mining (sha256_cuda.dll) - 50-200x faster than CPU
  + Auto-tuning GPU block/grid sizes
  + Double-buffering GPU pipeline
  + Dual pool support with interactive setup
  + Midstate-optimized C extension
  + Improved vardiff auto-adjustment
  + Better error recovery
"""

import os, sys, json, time, socket, hashlib, binascii, logging
import random, threading, platform, ctypes
import struct, sqlite3, subprocess, hmac
import multiprocessing as mp
from multiprocessing import Process, Queue, Value, Array
import numpy as np
from typing import List, Optional, Dict
import requests
import psutil

try:
    from gpu_miner import GPUMiner
    HAS_GPU_MODULE = True
except ImportError:
    HAS_GPU_MODULE = False

IS_WINDOWS = platform.system() == "Windows"
CPU_COUNT  = mp.cpu_count()
BTC_ADDRESS = ""
BCH_ADDRESS = ""
DIFF1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

TG_TOKEN = ""
TG_CHAT  = ""

# Pool definitions
POOLS = [
    {"name": "ckpool",  "host": "solo.ckpool.org",         "port": 3333},
    {"name": "braiins", "host": "solo.stratum.braiins.com", "port": 3333},
]

# BCH pool definitions
BCH_POOLS = [
    {"name": "solopool", "host": "bch.solopool.eu", "port": 3333},
]

if IS_WINDOWS:
    os.system("title TrueCryptoMiner v9 - by Truescent")

try:
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('ai_miner_v8.log', encoding='utf-8')]
)
LOG = logging.getLogger("AIMinerV8")


# ═══════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════

def fmt_hr(h):
    for u,d in [("EH/s",1e18),("PH/s",1e15),("TH/s",1e12),
                ("GH/s",1e9),("MH/s",1e6),("KH/s",1e3)]:
        if h>=d: return f"{h/d:.2f} {u}"
    return f"{h:.2f} H/s"

def fmt_share(diff):
    if diff<=0: return "0 H"
    h=diff*(2**32)
    for u,div in [("TH",1e12),("GH",1e9),("MH",1e6),("KH",1e3)]:
        if h>=div: return f"{h/div:.2f} {u}"
    return f"{h:.0f} H"

def safe_print(s):
    try: print(s)
    except: print(s.encode('ascii','ignore').decode('ascii'))

def nbits_to_target(nbits):
    try:
        n=int(nbits,16); return (n&0xffffff)*(2**(8*((n>>24)-3)))
    except: return 2**256-1

def target_to_hex64(t): return format(t,'064x')
def dsha(data): return hashlib.sha256(hashlib.sha256(data, usedforsecurity=False).digest(), usedforsecurity=False).digest()
def int_to_diff(h): return DIFF1/h if h>0 else 0.0


# ═══════════════════════════════════════════════════════════════
#  SYSTEM OPTIMISATION
# ═══════════════════════════════════════════════════════════════

def optimise_system():
    safe_print("[OPT] Applying system optimisations...")
    if IS_WINDOWS:
        try:
            subprocess.run(
                ['powercfg', '/setactive', '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'],
                capture_output=True)
            safe_print("[OPT] Power plan: High Performance activated")
        except: pass
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            subprocess.run([
                'powershell', '-Command',
                f'Add-MpPreference -ExclusionPath "{script_dir}"'
            ], capture_output=True)
            safe_print(f"[OPT] Windows Defender exclusion added for {script_dir}")
        except: pass
    try:
        proc = psutil.Process()
        if IS_WINDOWS:
            proc.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            proc.nice(-20)
        safe_print("[OPT] Main process priority: HIGH")
    except: pass


def boost_worker_priority(core_id: int):
    try:
        proc = psutil.Process()
        proc.cpu_affinity([core_id])
        if IS_WINDOWS:
            proc.nice(psutil.REALTIME_PRIORITY_CLASS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, 0x00000100)
        else:
            os.nice(-20)
            try: os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(99))
            except: pass
    except: pass


# ═══════════════════════════════════════════════════════════════
#  C EXTENSION LOADER
# ═══════════════════════════════════════════════════════════════

def load_c_engine(script_dir: str):
    candidates = [
        os.path.join(script_dir, 'sha256_miner.dll'),
        os.path.join(script_dir, 'sha256_miner.so'),
        'sha256_miner.dll',
        'sha256_miner.so',
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                lib = ctypes.CDLL(path)
                lib.mine_range.argtypes = [
                    ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                    ctypes.c_char_p, ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_int64), ctypes.c_char_p,
                ]
                lib.mine_range.restype = ctypes.c_int
                return lib, True
            except: pass
    return None, False


# ═══════════════════════════════════════════════════════════════
#  WORKER PROCESS
# ═══════════════════════════════════════════════════════════════

def worker_process(
    core_id, num_cores, script_dir,
    job_queue, result_queue,
    bandit_rewards, bandit_counts,
    best_hash_val, shutdown_flag,
    share_target,
):
    boost_worker_priority(core_id)
    lib, c_mode = load_c_engine(script_dir)

    NUM_REGIONS = 32
    NONCE_MAX   = int(2**32)
    REGION_W    = NONCE_MAX // NUM_REGIONS
    BATCH       = 65536

    current_job    = None
    current_prefix = None
    current_en2    = None

    total_hashes = 0
    best_int     = 2**256-1
    best_hex_str = "f"*64
    last_report  = time.time()

    def dsha_py(data):
        return hashlib.sha256(hashlib.sha256(data, usedforsecurity=False).digest(), usedforsecurity=False).digest()

    def build_prefix(job, en1, en2):
        try:
            cb  = job['coinb1']+en1+en2+job['coinb2']
            cbh = dsha_py(binascii.unhexlify(cb))
            m   = cbh
            for b in job['merkle_branch']:
                m = dsha_py(m+binascii.unhexlify(b))
            mr = binascii.hexlify(m).decode()
            mr = ''.join([mr[i:i+2] for i in range(0,len(mr),2)][::-1])
            p  = job['version']+job['prevhash']+mr+job['ntime']+job['nbits']
            return p if len(p)==152 else None
        except: return None

    def pick_region():
        import math
        best_ucb = -1
        best_r = 0
        total = sum(max(bandit_counts[i], 1e-9) for i in range(NUM_REGIONS))
        for i in range(NUM_REGIONS):
            c = max(bandit_counts[i], 1e-9)
            r = bandit_rewards[i]
            ucb = r/c + math.sqrt(2*math.log(max(total,1))/c)
            if ucb > best_ucb:
                best_ucb = ucb
                best_r = i
        return best_r

    def update_bandit(region, hash_int, target):
        reward = 1.0 if hash_int<=target else min(1.0,target/max(hash_int,1))
        n = bandit_counts[region]+1
        bandit_counts[region] = n
        bandit_rewards[region] += (reward-bandit_rewards[region])/n

    def mine_c(prefix, start, end, target_hex):
        out_hash      = ctypes.create_string_buffer(65)
        best_hash_buf = ctypes.create_string_buffer(65)
        out_nonce     = ctypes.c_int64(-1)
        found = lib.mine_range(
            prefix.encode('ascii'),
            ctypes.c_uint32(start % NONCE_MAX),
            ctypes.c_uint32(end   % NONCE_MAX),
            target_hex.encode('ascii'),
            out_hash, ctypes.byref(out_nonce), best_hash_buf,
        )
        best_h = best_hash_buf.value.decode('ascii')
        best_i = int(best_h,16) if best_h else 2**256-1
        if found and out_nonce.value>=0:
            return out_hash.value.decode('ascii'), int(out_nonce.value), best_i
        return None, None, best_i

    def mine_py(prefix, start, end, target):
        local_best = 2**256-1
        for nonce in range(start, min(end, NONCE_MAX)):
            hdr = prefix+format(nonce,'08x')
            try: raw=binascii.unhexlify(hdr)
            except: continue
            h=dsha_py(raw); hi=int.from_bytes(h[::-1],'big')
            if hi<local_best: local_best=hi
            if hi<=target:
                return format(hi,'064x'),nonce,local_best
        return None,None,local_best

    while not shutdown_flag.value:
        try:
            while True:
                new_job = job_queue.get_nowait()
                current_job = new_job
                # SV2 standard-channel jobs arrive with a fully built 76-byte
                # header prefix (no client-side coinbase/extranonce rolling).
                if new_job.get('_prebuilt_prefix'):
                    current_prefix = new_job['_prebuilt_prefix']
                    current_en2 = ''
                else:
                    en2_size = new_job.get('_en2_size', 4)
                    en2_val  = random.randint(0, 2**(8*en2_size)-1)
                    current_en2 = format(en2_val, f'0{2*en2_size}x')
                    current_prefix = build_prefix(
                        new_job, new_job.get('_en1',''), current_en2)
        except: pass

        if not current_job or not current_prefix:
            time.sleep(0.05); continue

        target_int = current_job.get('_target', 2**256-1)
        st_ratio = share_target.value
        if 0 < st_ratio <= 1.0:
            use_target = int(DIFF1 * st_ratio)
        else:
            use_target = target_int

        target_hex = target_to_hex64(use_target)

        region   = pick_region()
        reg_start = region * REGION_W
        slice_w  = REGION_W // num_cores
        start    = reg_start + core_id * slice_w
        end      = min(start + BATCH, reg_start + (core_id+1)*slice_w)
        if start >= reg_start + REGION_W:
            start = reg_start + (core_id * BATCH) % REGION_W
            end   = min(start + BATCH, reg_start + REGION_W)

        start = int(start) % NONCE_MAX
        end   = min(int(end), NONCE_MAX)
        if end <= start: end = min(start+BATCH, NONCE_MAX)

        try:
            t0 = time.time()
            if c_mode and lib:
                hash_hex, nonce, best_seen = mine_c(current_prefix, start, end, target_hex)
            else:
                hash_hex, nonce, best_seen = mine_py(current_prefix, start, end, use_target)
            elapsed = max(time.time()-t0, 1e-9)

            batch_count = end-start
            total_hashes += batch_count

            update_bandit(region, best_seen, use_target)

            if best_seen < best_hash_val.value:
                best_hash_val.value = float(best_seen)
                best_hex_str = format(best_seen,'064x')

            if not current_job.get('_prebuilt_prefix') and random.random() < 0.05:
                en2_size = current_job.get('_en2_size', 4)
                en2_val  = random.randint(0, 2**(8*en2_size)-1)
                current_en2 = format(en2_val, f'0{2*en2_size}x')
                current_prefix = build_prefix(
                    current_job, current_job.get('_en1',''), current_en2)

            if hash_hex and nonce is not None:
                result_queue.put({
                    'type':       'solution',
                    'core_id':    core_id,
                    'hash_hex':   hash_hex,
                    'nonce':      nonce,
                    'en2':        current_en2,
                    'job_id':     current_job['job_id'],
                    'ntime':      current_job['ntime'],
                    'best_int':   best_seen,
                    '_pool_id':   current_job.get('_pool_id', 0),
                })

            now = time.time()
            if now-last_report >= 5.0:
                hr = total_hashes/(now-last_report) if now>last_report else 0
                result_queue.put({
                    'type':      'stats',
                    'core_id':   core_id,
                    'hashes':    total_hashes,
                    'hashrate':  hr,
                    'best_int':  int(best_hash_val.value),
                    'best_hex':  best_hex_str,
                    'c_mode':    c_mode,
                    '_pool_name': current_job.get('_pool_name', 'unknown') if current_job else 'unknown',
                })
                total_hashes = 0
                last_report  = now

        except Exception as e:
            time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════
#  GPU WORKER PROCESS
# ═══════════════════════════════════════════════════════════════

def gpu_worker_process(
    script_dir, job_queue, result_queue,
    best_hash_val, shutdown_flag, share_target,
):
    """GPU mining process. Reads jobs from queue, mines on GPU, sends results."""
    try:
        from gpu_miner import GPUMiner
    except ImportError:
        safe_print("[GPU] gpu_miner.py not found. GPU worker exiting.")
        return

    gpu = GPUMiner(script_dir)
    # Suppress ALL C-level printf from DLL (init, autotune, setup, etc)
    def _suppress_c_output(func, *args, **kwargs):
        _dn = os.open(os.devnull, os.O_WRONLY)
        _old_fd = os.dup(1)
        os.dup2(_dn, 1)
        try:
            result = func(*args, **kwargs)
        finally:
            os.dup2(_old_fd, 1)
            os.close(_old_fd)
            os.close(_dn)
        return result

    ok = _suppress_c_output(gpu.init)
    if not ok:
        LOG.error("[GPU] GPU initialization failed")
        safe_print("[GPU] GPU initialization failed. GPU worker exiting.")
        return

    gpu.best_tpb = 128
    gpu.best_grid = 160
    LOG.info(f"[GPU] Initialized - tpb={gpu.best_tpb} grid={gpu.best_grid} threads={gpu.best_tpb*gpu.best_grid}")
    safe_print(f"[GPU] Ready ({gpu.best_tpb}x{gpu.best_grid} threads = {gpu.best_tpb*gpu.best_grid} threads)")

    current_job = None
    current_prefix = None
    current_en2 = None
    total_hashes = 0
    last_report = time.time()
    last_header_hex = None
    last_target_int = None

    while not shutdown_flag.value:
        try:
            while True:
                new_job = job_queue.get_nowait()
                current_job = new_job
                if new_job.get('_prebuilt_prefix'):
                    current_prefix = new_job['_prebuilt_prefix']
                    current_en2 = ''
                else:
                    en2_size = new_job.get('_en2_size', 4)
                    en2_val = random.randint(0, 2**(8*en2_size)-1)
                    current_en2 = format(en2_val, f'0{2*en2_size}x')
                    current_prefix = build_prefix_for_gpu(
                        new_job, new_job.get('_en1', ''), current_en2)
                last_header_hex = None
                LOG.info(f"[GPU] Received job {new_job.get('job_id', '?')} from {new_job.get('_pool_name', '?')}")
        except:
            pass

        if not current_job or not current_prefix:
            time.sleep(0.05)
            continue

        target_int = current_job.get('_target', 2**256-1)
        st_ratio = share_target.value
        if 0 < st_ratio <= 1.0:
            use_target = int(DIFF1 * st_ratio)
        else:
            use_target = target_int

        header_hex = current_prefix

        if header_hex != last_header_hex or use_target != last_target_int:
            safe_print(f"[GPU] Setup: target={use_target:#x}")
            last_header_hex = header_hex
            last_target_int = use_target

        try:
            t0 = time.time()

            found_nonces = gpu.mine_batch(
                header_hex, use_target,
                nonce_start=0, nonce_count=2**32 - 1)

            elapsed = max(time.time() - t0, 1e-9)

            gpu_hashrate = (2**32 - 1) / elapsed
            total_hashes += 2**32 - 1

            if random.random() < 0.05:
                safe_print(f"[GPU] {elapsed:.1f}s batch = {gpu_hashrate/1e6:.1f} MH/s | found={len(found_nonces)} solutions")

            for nonce in found_nonces:
                nonce_hex = format(nonce, '08x')
                full_hdr = header_hex + nonce_hex
                try:
                    raw = binascii.unhexlify(full_hdr)
                    h = hashlib.sha256(hashlib.sha256(raw, usedforsecurity=False).digest(), usedforsecurity=False).digest()
                    hash_int = int.from_bytes(h[::-1], 'big')
                    hash_hex = binascii.hexlify(h[::-1]).decode()
                except:
                    continue

                if hash_int > use_target:
                    safe_print(f"[GPU] Solution nonce={nonce_hex} hash below target but rejected in verification")
                    continue

                diff = int_to_diff(hash_int)
                safe_print(f"[GPU] SOLUTION FOUND! nonce={nonce_hex} diff={diff:.6f} hash={hash_hex[:16]}...")
                LOG.info(f"[GPU] Solution: nonce={nonce_hex} diff={diff:.6f}")

                if hash_int < best_hash_val.value:
                    best_hash_val.value = float(hash_int)

                result_queue.put({
                    'type': 'solution',
                    'core_id': 999,
                    'hash_hex': hash_hex,
                    'nonce': nonce,
                    'en2': current_en2,
                    'job_id': current_job['job_id'],
                    'ntime': current_job['ntime'],
                    'best_int': hash_int,
                    '_pool_id': current_job.get('_pool_id', 0),
                    '_pool_name': current_job.get('_pool_name', 'GPU'),
                })

            now = time.time()
            if now - last_report >= 30.0:
                gpu_best = int(best_hash_val.value)
                result_queue.put({
                    'type': 'stats',
                    'core_id': 999,
                    'hashes': total_hashes,
                    'hashrate': gpu_hashrate,
                    'best_int': gpu_best,
                    'best_hex': format(gpu_best, '064x'),
                    'c_mode': False,
                    '_pool_name': current_job.get('_pool_name', 'GPU') if current_job else 'GPU',
                    '_is_gpu': True,
                })
                total_hashes = 0
                last_report = now

            if not current_job.get('_prebuilt_prefix') and random.random() < 0.05:
                en2_size = current_job.get('_en2_size', 4)
                en2_val = random.randint(0, 2**(8*en2_size)-1)
                current_en2 = format(en2_val, f'0{2*en2_size}x')
                current_prefix = build_prefix_for_gpu(
                    current_job, current_job.get('_en1', ''), current_en2)

        except Exception as e:
            LOG.error(f"[GPU] Error: {e}")
            safe_print(f"[GPU] Error: {e}")
            time.sleep(1)

    gpu.cleanup()
    LOG.info("[GPU] Worker exited")
    safe_print("[GPU] GPU worker exited.")


def build_prefix_for_gpu(job, en1, en2):
    """Build the 76-byte header prefix for GPU mining (everything except nonce)."""
    try:
        cb = job['coinb1'] + en1 + en2 + job['coinb2']
        cbh = hashlib.sha256(hashlib.sha256(binascii.unhexlify(cb), usedforsecurity=False).digest(), usedforsecurity=False).digest()
        m = cbh
        for b in job['merkle_branch']:
            m = hashlib.sha256(m + binascii.unhexlify(b), usedforsecurity=False).digest()
        mr = binascii.hexlify(m).decode()
        mr = ''.join([mr[i:i+2] for i in range(0, len(mr), 2)][::-1])
        p = job['version'] + job['prevhash'] + mr + job['ntime'] + job['nbits']
        return p if len(p) == 152 else None
    except:
        return None

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
class TelegramAlert:
    def __init__(self, token, chat_id):
        self.token=token; self.chat_id=chat_id
        self.url=f"https://api.telegram.org/bot{token}/sendMessage"
        self.last_share=0.0; self.lock=threading.Lock()

    def send(self, msg):
        threading.Thread(target=self._send, args=(msg,), daemon=True).start()

    def _send(self, msg):
        try:
            requests.post(self.url, json={'chat_id':self.chat_id,'text':msg,'parse_mode':'HTML'}, timeout=10)
        except: pass

    def notify_startup(self, cores, c_mode, pools):
        pool_str = " + ".join([f"{p.name}:{p.host}:{p.port}" for p in pools])
        self.send(f"AI Miner v8 Started\n"
                  f"Addr: {BTC_ADDRESS}\n"
                  f"Cores: {cores}\n"
                  f"Engine: {'C Extension' if c_mode else 'Python'}\n"
                  f"Pools: {pool_str}\n"
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}")

    def notify_best_share(self, diff, size_str, hash_hex, pool_name="unknown", engine="CPU"):
        now=time.time()
        with self.lock:
            if now-self.last_share<30: return
            self.last_share=now
        self.send(f"Best Share Found!\n"
                  f"Pool: {pool_name}\n"
                  f"Engine: {engine}\n"
                  f"Diff: {diff:.6f} ({size_str})\n"
                  f"Hash: {hash_hex[:32]}...\n"
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}")

    def notify_block(self, hash_hex, nonce, pool_name, engine="CPU"):
        self.send(f"BLOCK FOUND!!!\n"
                  f"Pool: {pool_name}\n"
                  f"Engine: {engine}\n"
                  f"Hash: {hash_hex}\n"
                  f"Nonce: {nonce}\n"
                  f"Addr: {BTC_ADDRESS}\n"
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}")

    def notify_share_accepted(self, pool_name, total, engine="CPU"):
        if total==1 or total%10==0:
            self.send(f"Share Accepted!\n"
                      f"Pool: {pool_name}\n"
                      f"Engine: {engine}\n"
                      f"Total: {total}\n"
                      f"{time.strftime('%H:%M:%S')}")


# ═══════════════════════════════════════════════════════════════
#  STRATUM PROTOCOL DETECTION (sv1 / sv2)
# ═══════════════════════════════════════════════════════════════

# URL scheme prefixes used to explicitly request a Stratum protocol on a
# per-pool basis. Anything else is auto-detected at connect time.
SV2_SCHEMES = ("stratum2+tcp://", "stratum2://", "sv2+tcp://", "sv2://")
SV1_SCHEMES = ("stratum+tcp://", "stratum1+tcp://", "stratum://", "tcp://")


def parse_pool_scheme(host):
    """Split an optional protocol scheme off a pool host.

    Returns (clean_host, hint) where hint is 'sv2', 'sv1' or 'auto'.
    Existing plain hostnames (no scheme) resolve to 'auto' so behaviour is
    unchanged for pools configured the classic way.
    """
    h = (host or "").strip()
    low = h.lower()
    for sch in SV2_SCHEMES:
        if low.startswith(sch):
            return h[len(sch):], "sv2"
    for sch in SV1_SCHEMES:
        if low.startswith(sch):
            return h[len(sch):], "sv1"
    return h, "auto"


def sv2_ephemeral_pubkey():
    """Return a 32-byte X25519 ephemeral public key for the sv2 Noise
    handshake initiation. Uses `cryptography` when available; otherwise a
    random 32-byte value, which is sufficient for a detection-only probe
    because the sv2 responder replies with its own handshake message before
    verifying anything."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        priv = X25519PrivateKey.generate()
        return priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except Exception:
        return os.urandom(32)


# ═══════════════════════════════════════════════════════════════
#  STRATUM V2 (SV2) - Noise transport + binary codec + mining
# ═══════════════════════════════════════════════════════════════
#
# This block implements a pure-Python Stratum V2 client good enough to open a
# STANDARD mining channel and mine on it. The design goals are:
#   * Coin-agnostic transport/codec (BTC, BC2/BitcoinII, BCH, bch2, ...).
#   * Reuse of the existing SHA-256d worker/hashing/submit pipeline.
#   * Safe, transparent fallback to the proven sv1 path on ANY failure.
#
# Handshake variant implemented: Noise_NX_25519_ChaChaPoly_SHA256
#   (X25519 ECDH, ChaCha20-Poly1305 AEAD, SHA-256, HKDF). This is the classic
#   SV2 handshake and the one implementable with the `cryptography` package.
#   NOTE: the current sv2-spec has since migrated to a secp256k1+EllSwift
#   variant which is not implementable in pure Python without libsecp256k1;
#   pools still speaking the 25519 handshake work, others fall back to sv1.
#
# The pool's authority public key is used only for OPTIONAL certificate
# verification (server authentication). Because NX transmits the responder's
# static key during the handshake, the encrypted channel is established even
# without the authority key; verification is best-effort and never fatal.

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization as _crypto_ser
    HAS_CRYPTOGRAPHY = True
except Exception:
    HAS_CRYPTOGRAPHY = False

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAS_ED25519 = True
except Exception:
    HAS_ED25519 = False

# Known SV2 authority key for mkpool BitcoinII (bc2). Used as a default
# pre-fill only; never assumed for other pools.
MKPOOL_BC2_AUTHORITY_KEY = "9c9aZWzETaiJyqGGUSCn8GqFgTpxs96ert4d4jGeRnvxqRqhZar"

# SV2 message type identifiers (from the sv2-spec).
SV2_MT_SETUP_CONNECTION                 = 0x00
SV2_MT_SETUP_CONNECTION_SUCCESS         = 0x01
SV2_MT_SETUP_CONNECTION_ERROR           = 0x02
SV2_MT_CHANNEL_ENDPOINT_CHANGED         = 0x03
SV2_MT_OPEN_STANDARD_MINING_CHANNEL     = 0x10
SV2_MT_OPEN_STANDARD_MINING_CHANNEL_OK  = 0x11
SV2_MT_OPEN_MINING_CHANNEL_ERROR        = 0x12
SV2_MT_UPDATE_CHANNEL                   = 0x16
SV2_MT_UPDATE_CHANNEL_ERROR             = 0x17
SV2_MT_CLOSE_CHANNEL                    = 0x18
SV2_MT_SET_EXTRANONCE_PREFIX            = 0x19
SV2_MT_SUBMIT_SHARES_STANDARD           = 0x1a
SV2_MT_SUBMIT_SHARES_EXTENDED           = 0x1b
SV2_MT_SUBMIT_SHARES_SUCCESS            = 0x1c
SV2_MT_SUBMIT_SHARES_ERROR              = 0x1d
SV2_MT_NEW_MINING_JOB                   = 0x1e
SV2_MT_NEW_EXTENDED_MINING_JOB          = 0x1f
SV2_MT_SET_NEW_PREV_HASH                = 0x20
SV2_MT_SET_TARGET                       = 0x21
SV2_MT_RECONNECT                        = 0x25

SV2_CHANNEL_MSG_BIT = 0x8000
# SetupConnection mining-protocol flag: client only understands standard jobs.
SV2_FLAG_REQUIRES_STANDARD_JOBS = 0x00000001


class Sv2Error(Exception):
    pass


# ── SV2 primitive (de)serialisation. All multi-byte ints little-endian. ──

def _sv2_u8(v):   return struct.pack('<B', v & 0xff)
def _sv2_u16(v):  return struct.pack('<H', v & 0xffff)
def _sv2_u24(v):
    v &= 0xffffff
    return bytes((v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff))
def _sv2_u32(v):  return struct.pack('<I', v & 0xffffffff)
def _sv2_u64(v):  return struct.pack('<Q', v & 0xffffffffffffffff)
def _sv2_f32(v):  return struct.pack('<f', float(v))
def _sv2_u256(v):
    """int -> 32-byte little-endian U256."""
    return int(v).to_bytes(32, 'little')
def _sv2_str0_255(s):
    b = s.encode('utf-8') if isinstance(s, str) else bytes(s)
    b = b[:255]
    return _sv2_u8(len(b)) + b
def _sv2_b0_32(b):
    b = bytes(b)[:32]
    return _sv2_u8(len(b)) + b
def _sv2_option_u32(v):
    return _sv2_u8(0) if v is None else (_sv2_u8(1) + _sv2_u32(v))


class _Sv2Reader:
    """Cursor-based reader for SV2 serialized payloads."""
    def __init__(self, data):
        self.d = data
        self.i = 0
    def take(self, n):
        if self.i + n > len(self.d):
            raise Sv2Error("sv2 payload short read")
        r = self.d[self.i:self.i + n]
        self.i += n
        return r
    def u8(self):   return self.take(1)[0]
    def u16(self):  return struct.unpack('<H', self.take(2))[0]
    def u24(self):
        b = self.take(3)
        return b[0] | (b[1] << 8) | (b[2] << 16)
    def u32(self):  return struct.unpack('<I', self.take(4))[0]
    def u64(self):  return struct.unpack('<Q', self.take(8))[0]
    def f32(self):  return struct.unpack('<f', self.take(4))[0]
    def u256(self): return self.take(32)            # raw 32 bytes (LE order)
    def str0_255(self):
        return self.take(self.u8()).decode('utf-8', 'ignore')
    def b0_32(self):
        return self.take(self.u8())
    def option_u32(self):
        return self.u32() if self.u8() != 0 else None


def sv2_frame_header(msg_type, payload_len, channel_msg=False):
    """Build the 6-byte SV2 frame header (ext_type U16, msg_type U8, len U24)."""
    ext = SV2_CHANNEL_MSG_BIT if channel_msg else 0x0000
    return _sv2_u16(ext) + _sv2_u8(msg_type) + _sv2_u24(payload_len)


def sv2_parse_frame_header(hdr6):
    ext = struct.unpack('<H', hdr6[0:2])[0]
    msg_type = hdr6[2]
    length = hdr6[3] | (hdr6[4] << 8) | (hdr6[5] << 16)
    return ext, msg_type, length


# ── SV2 message builders (only what a standard-channel miner needs) ──

def sv2_msg_setup_connection(endpoint_host, endpoint_port,
                             flags=0, protocol=0,
                             vendor="TrueCryptoMiner", firmware="v9"):
    p  = _sv2_u8(protocol)
    p += _sv2_u16(2)                       # min_version
    p += _sv2_u16(2)                       # max_version
    p += _sv2_u32(flags)
    p += _sv2_str0_255(endpoint_host)
    p += _sv2_u16(endpoint_port)
    p += _sv2_str0_255(vendor)
    p += _sv2_str0_255("")                 # hardware_version
    p += _sv2_str0_255(firmware)
    p += _sv2_str0_255("")                 # device_id
    return p


def sv2_msg_open_standard_channel(request_id, user_identity,
                                  nominal_hash_rate, max_target_int):
    p  = _sv2_u32(request_id)
    p += _sv2_str0_255(user_identity)
    p += _sv2_f32(nominal_hash_rate)
    p += _sv2_u256(max_target_int)
    return p


def sv2_msg_submit_shares_standard(channel_id, sequence_number, job_id,
                                   nonce, ntime, version):
    p  = _sv2_u32(channel_id)
    p += _sv2_u32(sequence_number)
    p += _sv2_u32(job_id)
    p += _sv2_u32(nonce)
    p += _sv2_u32(ntime)
    p += _sv2_u32(version)
    return p


# ── SV2 message parsers ──

def sv2_parse_setup_connection_success(payload):
    r = _Sv2Reader(payload)
    return {"used_version": r.u16(), "flags": r.u32()}


def sv2_parse_setup_connection_error(payload):
    r = _Sv2Reader(payload)
    return {"flags": r.u32(), "error_code": r.str0_255()}


def sv2_parse_open_standard_channel_success(payload):
    r = _Sv2Reader(payload)
    return {
        "request_id":        r.u32(),
        "channel_id":        r.u32(),
        "target":            int.from_bytes(r.u256(), 'little'),
        "extranonce_prefix": r.b0_32(),
        "group_channel_id":  r.u32(),
    }


def sv2_parse_open_channel_error(payload):
    r = _Sv2Reader(payload)
    return {"request_id": r.u32(), "error_code": r.str0_255()}


def sv2_parse_new_mining_job(payload):
    r = _Sv2Reader(payload)
    return {
        "channel_id":  r.u32(),
        "job_id":      r.u32(),
        "min_ntime":   r.option_u32(),
        "version":     r.u32(),
        "merkle_root": r.u256(),          # raw 32 bytes, header order
    }


def sv2_parse_set_new_prev_hash(payload):
    r = _Sv2Reader(payload)
    return {
        "channel_id": r.u32(),
        "job_id":     r.u32(),
        "prev_hash":  r.u256(),           # raw 32 bytes, header order
        "min_ntime":  r.u32(),
        "nbits":      r.u32(),
    }


def sv2_parse_set_target(payload):
    r = _Sv2Reader(payload)
    return {
        "channel_id":     r.u32(),
        "maximum_target": int.from_bytes(r.u256(), 'little'),
    }


def sv2_parse_submit_shares_success(payload):
    r = _Sv2Reader(payload)
    return {
        "channel_id":                 r.u32(),
        "last_sequence_number":       r.u32(),
        "new_submits_accepted_count": r.u32(),
        "new_shares_sum":             r.u64(),
    }


def sv2_parse_submit_shares_error(payload):
    r = _Sv2Reader(payload)
    return {
        "channel_id":      r.u32(),
        "sequence_number": r.u32(),
        "error_code":      r.str0_255(),
    }


def nbits_int_to_target(nbits):
    """Compact-bits (as an int, e.g. from SV2 U32) -> full 256-bit target."""
    exp  = (nbits >> 24) & 0xff
    mant = nbits & 0xffffff
    if exp <= 3:
        return mant >> (8 * (3 - exp))
    return mant << (8 * (exp - 3))


def byteswap_u32(n):
    """Reinterpret the 4 big-endian bytes of n as a little-endian U32.

    The worker builds headers with the nonce appended as big-endian hex, so
    the 4 header nonce bytes are n.to_bytes(4,'big'). SV2 SubmitSharesStandard
    carries the nonce as a U32 whose little-endian serialization must equal
    those same 4 bytes, hence this swap keeps the submitted header identical
    to the one that was actually hashed.
    """
    return int.from_bytes(int(n).to_bytes(4, 'big'), 'little')


# ── base58 / authority key handling (best-effort, verification only) ──

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_decode(s):
    num = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise Sv2Error(f"invalid base58 char {ch!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, 'big') if num else b''
    n_pad = len(s) - len(s.lstrip('1'))
    return b'\x00' * n_pad + body


def decode_authority_pubkey(key_str):
    """Best-effort decode of a base58(-check) authority key to its 32-byte
    public key. Returns None if it cannot be interpreted. Never raises."""
    if not key_str:
        return None
    try:
        raw = base58_decode(key_str.strip())
    except Exception:
        return None
    # Strip a trailing 4-byte base58check checksum when present.
    if len(raw) in (36, 38):
        raw = raw[:-4]
    # Strip a leading version prefix (commonly 2 bytes, e.g. [1,0]).
    if len(raw) == 34:
        raw = raw[2:]
    elif len(raw) > 32:
        raw = raw[-32:]
    return raw if len(raw) == 32 else None


# ── Noise cipher / handshake state ──

class Sv2CipherState:
    """Post-handshake AEAD state with a monotonically increasing 64-bit nonce
    (per Noise: 4 zero bytes || little-endian counter)."""
    def __init__(self, key):
        self.key = key
        self.n = 0
        self.aead = ChaCha20Poly1305(key) if (key and HAS_CRYPTOGRAPHY) else None

    def _nonce(self):
        return b'\x00\x00\x00\x00' + self.n.to_bytes(8, 'little')

    def encrypt(self, ad, pt):
        if self.aead is None:
            return pt
        ct = self.aead.encrypt(self._nonce(), pt, ad)
        self.n += 1
        return ct

    def decrypt(self, ad, ct):
        if self.aead is None:
            return ct
        pt = self.aead.decrypt(self._nonce(), ct, ad)
        self.n += 1
        return pt


class Sv2Handshake:
    """Symmetric+handshake state for Noise_NX_25519_ChaChaPoly_SHA256."""
    PROTOCOL_NAME = b"Noise_NX_25519_ChaChaPoly_SHA256"   # exactly 32 bytes

    def __init__(self):
        # protocolName is exactly 32 bytes -> used directly as initial h.
        self.h = self.PROTOCOL_NAME
        self.ck = self.h
        self.h = hashlib.sha256(self.h).digest()   # MixHash(empty prologue)
        self.k = None
        self.n = 0

    def mix_hash(self, data):
        self.h = hashlib.sha256(self.h + data).digest()

    @staticmethod
    def _hkdf2(ck, ikm):
        tk = hmac.new(ck, ikm, hashlib.sha256).digest()
        o1 = hmac.new(tk, b'\x01', hashlib.sha256).digest()
        o2 = hmac.new(tk, o1 + b'\x02', hashlib.sha256).digest()
        return o1, o2

    def mix_key(self, ikm):
        self.ck, self.k = self._hkdf2(self.ck, ikm)
        self.n = 0

    def _nonce(self):
        return b'\x00\x00\x00\x00' + self.n.to_bytes(8, 'little')

    def encrypt_and_hash(self, pt):
        if self.k is None:
            self.mix_hash(pt)
            return pt
        ct = ChaCha20Poly1305(self.k).encrypt(self._nonce(), pt, self.h)
        self.n += 1
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ct):
        if self.k is None:
            self.mix_hash(ct)
            return ct
        pt = ChaCha20Poly1305(self.k).decrypt(self._nonce(), ct, self.h)
        self.n += 1
        self.mix_hash(ct)
        return pt

    def split(self):
        t1, t2 = self._hkdf2(self.ck, b'')
        return Sv2CipherState(t1), Sv2CipherState(t2)


def _x25519_pub_bytes(priv):
    return priv.public_key().public_bytes(
        _crypto_ser.Encoding.Raw, _crypto_ser.PublicFormat.Raw)


def _x25519_dh(priv, peer_pub_bytes):
    return priv.exchange(X25519PublicKey.from_public_bytes(peer_pub_bytes))


class Sv2Transport:
    """Framed, Noise-encrypted SV2 transport over a TCP socket.

    After handshake_initiator() succeeds, send_message()/recv_message()
    exchange SV2 frames encrypted per sv2-spec section 4.6 (header encrypted
    as a 22-byte block, payload encrypted in <=65519-byte chunks)."""

    MAX_PT = 65519
    MAX_CT = 65535
    MAC    = 16

    def __init__(self, sock):
        self.sock = sock
        self._buf = b''
        self.send_cs = None      # initiator -> responder
        self.recv_cs = None      # responder -> initiator
        self.rs_pubkey = None    # server static public key (from handshake)
        self.cert_verified = False

    # -- low level byte IO --
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Sv2Error("sv2 connection closed")
            self._buf += chunk
        r, self._buf = self._buf[:n], self._buf[n:]
        return r

    def settimeout(self, t):
        try:
            self.sock.settimeout(t)
        except Exception:
            pass

    # -- Noise NX handshake (initiator) --
    def handshake_initiator(self, authority_key=None):
        if not HAS_CRYPTOGRAPHY:
            raise Sv2Error("cryptography package required for sv2 handshake")
        hs = Sv2Handshake()
        e_priv = X25519PrivateKey.generate()
        e_pub = _x25519_pub_bytes(e_priv)
        hs.mix_hash(e_pub)
        hs.encrypt_and_hash(b'')                      # empty payload (k empty)
        self.sock.sendall(e_pub)                      # Act 1: -> e (32 bytes)

        msg = self._recv_exact(170)                   # Act 2: 32 + 48 + 90
        re_pub = msg[0:32]
        hs.mix_hash(re_pub)
        hs.mix_key(_x25519_dh(e_priv, re_pub))        # ee
        rs_pub = hs.decrypt_and_hash(msg[32:80])      # s  (32 + 16 MAC)
        hs.mix_key(_x25519_dh(e_priv, rs_pub))        # es
        sig_msg = hs.decrypt_and_hash(msg[80:170])    # SIGNATURE_NOISE_MESSAGE

        self.rs_pubkey = rs_pub
        self.send_cs, self.recv_cs = hs.split()
        self.cert_verified = self._verify_certificate(
            authority_key, rs_pub, sig_msg)
        return True

    def _verify_certificate(self, authority_key, server_static, sig_msg):
        """Optional server authentication. Non-fatal: returns True/False and
        never raises. The 25519-era certificate signature is Ed25519 over
        (version||valid_from||not_valid_after||server_static_pubkey)."""
        ak = decode_authority_pubkey(authority_key)
        if ak is None or not HAS_ED25519 or len(sig_msg) < 10:
            return False
        try:
            r = _Sv2Reader(sig_msg)
            version = r.u16()
            valid_from = r.u32()
            not_valid_after = r.u32()
            signature = r.take(64)
            signed = (_sv2_u16(version) + _sv2_u32(valid_from)
                      + _sv2_u32(not_valid_after) + server_static)
            Ed25519PublicKey.from_public_bytes(ak).verify(signature, signed)
            return True
        except Exception:
            return False

    # -- framed encrypted messaging --
    @staticmethod
    def _pt_len_to_ct_len(pt_len):
        remainder = pt_len % Sv2Transport.MAX_PT
        if remainder > 0:
            remainder += Sv2Transport.MAC
        return pt_len // Sv2Transport.MAX_PT * Sv2Transport.MAX_CT + remainder

    def send_message(self, msg_type, payload, channel_msg=False):
        header = sv2_frame_header(msg_type, len(payload), channel_msg)
        out = self.send_cs.encrypt(b'', header)          # 6 -> 22 bytes
        off = 0
        if not payload:
            self.sock.sendall(out)
            return
        while off < len(payload):
            chunk = payload[off:off + self.MAX_PT]
            out += self.send_cs.encrypt(b'', chunk)
            off += self.MAX_PT
        self.sock.sendall(out)

    def recv_message(self):
        """Returns (msg_type, payload). Blocks until a full frame arrives."""
        enc_header = self._recv_exact(6 + self.MAC)      # 22 bytes
        header = self.recv_cs.decrypt(b'', enc_header)
        _ext, msg_type, length = sv2_parse_frame_header(header)
        payload = b''
        if length:
            ct_len = self._pt_len_to_ct_len(length)
            enc_payload = self._recv_exact(ct_len)
            off = 0
            while off < len(enc_payload):
                take = min(self.MAX_CT, len(enc_payload) - off)
                payload += self.recv_cs.decrypt(b'', enc_payload[off:off + take])
                off += take
        return msg_type, payload


def sv2_noise_responder_reply(recv_exact, send_all, static_priv, sig_msg):
    """Responder side of the NX handshake. Used ONLY by the offline self-test
    to validate the initiator implementation end-to-end. Returns the responder
    (send_cs, recv_cs) pair, where the responder sends with recv_cs and
    receives with send_cs (mirror of the initiator)."""
    hs = Sv2Handshake()
    re_pub = recv_exact(32)
    hs.mix_hash(re_pub)
    hs.decrypt_and_hash(b'')
    e_priv = X25519PrivateKey.generate()
    e_pub = _x25519_pub_bytes(e_priv)
    out = e_pub
    hs.mix_hash(e_pub)
    hs.mix_key(_x25519_dh(e_priv, re_pub))               # ee
    out += hs.encrypt_and_hash(_x25519_pub_bytes(static_priv))   # s
    hs.mix_key(_x25519_dh(static_priv, re_pub))          # es
    out += hs.encrypt_and_hash(sig_msg)
    send_all(out)
    c1, c2 = hs.split()
    return c1, c2


def sv2_selftest():
    """Offline validation of the sv2 codec, Noise NX handshake and encrypted
    transport. Runs the initiator against an in-process responder over a local
    socket pair; needs no network. Returns True on success."""
    ok = True

    # ---- binary codec round-trips ----
    try:
        assert sv2_parse_frame_header(
            sv2_frame_header(SV2_MT_NEW_MINING_JOB, 100, True)) == \
            (SV2_CHANNEL_MSG_BIT, SV2_MT_NEW_MINING_JOB, 100)
        assert sv2_parse_frame_header(
            sv2_frame_header(SV2_MT_SUBMIT_SHARES_STANDARD, 0xffffff, False))[2] == 0xffffff

        mr = bytes(range(32))
        njm = (_sv2_u32(7) + _sv2_u32(42) + _sv2_option_u32(123456)
               + _sv2_u32(0x20000000) + mr)
        j = sv2_parse_new_mining_job(njm)
        assert (j['channel_id'] == 7 and j['job_id'] == 42
                and j['min_ntime'] == 123456 and j['version'] == 0x20000000
                and j['merkle_root'] == mr)

        njm2 = (_sv2_u32(7) + _sv2_u32(43) + _sv2_option_u32(None)
                + _sv2_u32(1) + mr)
        assert sv2_parse_new_mining_job(njm2)['min_ntime'] is None

        ph = (_sv2_u32(7) + _sv2_u32(42) + bytes(range(32))
              + _sv2_u32(1700000000) + _sv2_u32(0x1d00ffff))
        p = sv2_parse_set_new_prev_hash(ph)
        assert p['nbits'] == 0x1d00ffff and p['min_ntime'] == 1700000000

        ss = sv2_msg_submit_shares_standard(7, 1, 42, 0xdeadbeef, 1700000000, 0x20000000)
        r = _Sv2Reader(ss)
        assert (r.u32() == 7 and r.u32() == 1 and r.u32() == 42
                and r.u32() == 0xdeadbeef and r.u32() == 1700000000
                and r.u32() == 0x20000000)

        assert nbits_int_to_target(0x1d00ffff) == DIFF1
        assert byteswap_u32(0x01020304) == 0x04030201
        safe_print("[SELFTEST] codec: PASS")
    except Exception as e:
        ok = False
        safe_print(f"[SELFTEST] codec: FAIL {e}")

    # ---- Noise NX handshake + encrypted transport ----
    if not HAS_CRYPTOGRAPHY:
        safe_print("[SELFTEST] noise: SKIP ('cryptography' not installed)")
    else:
        a = b = None
        try:
            a, b = socket.socketpair()
            static = X25519PrivateKey.generate()
            sig_msg = _sv2_u16(0) + _sv2_u32(0) + _sv2_u32(0xffffffff) + bytes(64)
            result = {}

            def _responder():
                def rx(n):
                    d = b''
                    while len(d) < n:
                        c = b.recv(n - len(d))
                        if not c:
                            raise Sv2Error("closed")
                        d += c
                    return d
                c1, c2 = sv2_noise_responder_reply(rx, b.sendall, static, sig_msg)
                result['c1'], result['c2'] = c1, c2

            th = threading.Thread(target=_responder)
            th.start()

            tr = Sv2Transport(a)
            tr.handshake_initiator(None)
            th.join(timeout=10)
            if 'c1' not in result:
                raise Sv2Error("responder handshake did not complete")

            resp_tr = Sv2Transport(b)
            resp_tr.recv_cs = result['c1']   # initiator -> responder
            resp_tr.send_cs = result['c2']   # responder -> initiator

            payload = b"hello sv2 world" * 10
            tr.send_message(SV2_MT_SETUP_CONNECTION, payload)
            mt, got = resp_tr.recv_message()
            assert mt == SV2_MT_SETUP_CONNECTION and got == payload

            payload2 = os.urandom(200)
            resp_tr.send_message(SV2_MT_NEW_MINING_JOB, payload2, channel_msg=True)
            mt2, got2 = tr.recv_message()
            assert mt2 == SV2_MT_NEW_MINING_JOB and got2 == payload2

            safe_print("[SELFTEST] noise handshake + transport: PASS")
        except Exception as e:
            ok = False
            safe_print(f"[SELFTEST] noise: FAIL {e}")
        finally:
            for s in (a, b):
                if s is not None:
                    try: s.close()
                    except: pass

    ak = decode_authority_pubkey(MKPOOL_BC2_AUTHORITY_KEY)
    safe_print(f"[SELFTEST] mkpool bc2 authority key: "
               f"{'decoded ' + str(len(ak)) + ' bytes' if ak else 'could not decode'}")
    safe_print(f"[SELFTEST] RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ═══════════════════════════════════════════════════════════════
#  POOL CONNECTION
# ═══════════════════════════════════════════════════════════════

class PoolConnection:
    def __init__(self, pool_id, name, host, port, address, authority_key=None):
        self.pool_id  = pool_id
        self.name     = name
        # A pool URL may carry an explicit protocol scheme (e.g.
        # "stratum2+tcp://host") and, for sv2, an authority key in the path
        # (e.g. "stratum2+tcp://host/AUTHKEY"). Strip both and remember the
        # hint so we can negotiate sv2 vs sv1 automatically at connect time.
        clean_host, self.protocol_hint = parse_pool_scheme(host)
        if '/' in clean_host:
            clean_host, _, path = clean_host.partition('/')
            if path and not authority_key:
                authority_key = path
        self.host     = clean_host
        self.port     = port
        self.address  = address
        # SV2 authority (Noise static) public key for optional server auth.
        self.authority_key = authority_key
        # SV2 live-session state (populated by _connect_sv2_once()).
        self.sv2: Optional[Sv2Transport] = None
        self.sv2_channel_id   = 0
        self.sv2_extranonce   = b''
        self.sv2_target       = 0
        self.sv2_prevhash     = None
        self.sv2_jobs_meta    = {}      # job_id -> parsed NewMiningJob dict
        self.sv2_submit_meta  = {}      # job_id -> {version, ntime, channel_id}
        self.sv2_seq          = 0
        self.sock: Optional[socket.socket] = None
        self.extranonce1      = ''
        self.extranonce2_size = 4
        self.pool_diff: float = 1.0
        self.share_target: int = 2**256-1
        self.job_data: dict = {}
        self.connected = False
        # Negotiated Stratum protocol for the live connection: "sv1" or "sv2".
        # Defaults to sv1 (the classic path) until detection says otherwise.
        self.protocol = "sv1"
        self.sv2_detected = False
        self.lock = threading.Lock()

    def connect(self) -> bool:
        """Connect to the pool, auto-negotiating Stratum V1 vs V2.

        Ordering is chosen to be conservative:
          * A URL that explicitly requests sv2 tries sv2 first, then falls
            back to sv1 (many sv2 pools also expose an sv1 endpoint).
          * Otherwise the proven sv1 path is attempted first (identical to
            the classic behaviour, no added latency for existing pools) and
            sv2 detection is only attempted if sv1 fails.
        The negotiated protocol is stored in self.protocol.
        """
        for attempt in range(10):
            try:
                if self.protocol_hint == "sv2":
                    if self._connect_sv2_once():
                        return True
                    if self._connect_sv1_once():
                        return True
                else:
                    if self._connect_sv1_once():
                        return True
                    if self._connect_sv2_once():
                        return True
                raise ConnectionError("stratum handshake failed")
            except Exception as e:
                wait = min(120, 5*(2**attempt))
                safe_print(f"[POOL:{self.name}] Retry in {wait}s... ({e})")
                time.sleep(wait)
        return False

    def _connect_sv1_once(self) -> bool:
        """Classic Stratum V1 handshake (mining.subscribe / mining.authorize).

        Behaviour is intentionally identical to the original implementation;
        this is the fully-supported live mining path. Raises on failure so
        the retry/backoff loop in connect() handles it.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(60)
        s.connect((self.host, self.port))
        s.sendall((json.dumps({"id":1,"method":"mining.subscribe",
            "params":["AISoloMinerV8/8.0"]})+"\n").encode())
        resp = json.loads(s.recv(4096).decode().split('\n')[0])
        if resp.get('result'):
            self.extranonce1      = resp['result'][1]
            self.extranonce2_size = resp['result'][2]
        s.sendall((json.dumps({"id":2,"method":"mining.authorize",
            "params":[self.address,"x"]})+"\n").encode())
        s.recv(4096)
        self.sock = s
        self.connected = True
        self.protocol = "sv1"
        safe_print(f"[POOL:{self.name}] Connected to {self.host}:{self.port} (sv1)")
        LOG.info(f"Connected {self.name} {self.host}:{self.port} protocol=sv1")
        return True

    def _probe_sv2(self, timeout=4.0) -> bool:
        """Best-effort probe to decide whether a pool speaks Stratum V2.

        Stratum V2 opens with a Noise handshake whose first (initiator)
        message is a raw 32-byte X25519 ephemeral public key. We send that
        and inspect the reply: an sv2 responder answers with its own binary
        handshake message, whereas an sv1 endpoint speaks line-based JSON-RPC
        (or does not answer binary at all). Uses a throwaway socket so the
        real sv1 handshake, if needed, starts clean.
        """
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((self.host, self.port))
            s.sendall(sv2_ephemeral_pubkey())
            resp = s.recv(1024)
            if not resp:
                return False
            stripped = resp.lstrip()
            # JSON text => classic sv1, not sv2.
            if stripped[:1] in (b'{', b'['):
                return False
            # A plausible sv2 Noise handshake response is binary and carries
            # at least the responder's 32-byte ephemeral key.
            return len(resp) >= 32
        except Exception:
            return False
        finally:
            if s is not None:
                try: s.close()
                except: pass

    def _connect_sv2_once(self) -> bool:
        """Open a live Stratum V2 standard-channel mining session.

        Performs the Noise_NX_25519_ChaChaPoly_SHA256 handshake, sends
        SetupConnection, opens a standard mining channel and stores the
        encrypted transport + channel state on this object. Received jobs are
        wired into the existing hashing pipeline by listen_for_jobs_sv2().

        Returns True on a live sv2 session, or False (so connect() falls back
        to the proven sv1 path) if `cryptography` is missing, the pool does
        not speak the 25519 handshake, or any negotiation step fails.
        """
        if not HAS_CRYPTOGRAPHY:
            safe_print(f"[POOL:{self.name}] sv2 needs the 'cryptography' package; falling back to sv1")
            LOG.warning(f"Pool {self.name}: sv2 unavailable (no cryptography), protocol stays sv1")
            return False

        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect((self.host, self.port))

            transport = Sv2Transport(s)
            transport.handshake_initiator(self.authority_key)
            auth_str = "authority key VERIFIED" if transport.cert_verified else \
                       ("authority key not verified" if self.authority_key else "no authority key")
            safe_print(f"[POOL:{self.name}] sv2 Noise handshake OK ({auth_str})")

            # --- SetupConnection (Mining Protocol) ---
            transport.send_message(
                SV2_MT_SETUP_CONNECTION,
                sv2_msg_setup_connection(self.host, self.port,
                                         flags=SV2_FLAG_REQUIRES_STANDARD_JOBS))
            mt, payload = transport.recv_message()
            if mt == SV2_MT_SETUP_CONNECTION_ERROR:
                info = sv2_parse_setup_connection_error(payload)
                LOG.warning(f"Pool {self.name}: SetupConnection.Error {info}")
                safe_print(f"[POOL:{self.name}] sv2 SetupConnection error: {info.get('error_code')}")
                raise Sv2Error("SetupConnection rejected")
            if mt != SV2_MT_SETUP_CONNECTION_SUCCESS:
                raise Sv2Error(f"unexpected reply to SetupConnection: 0x{mt:02x}")

            # --- OpenStandardMiningChannel ---
            user_identity = f"{self.address}.worker"
            transport.send_message(
                SV2_MT_OPEN_STANDARD_MINING_CHANNEL,
                sv2_msg_open_standard_channel(
                    request_id=1, user_identity=user_identity,
                    nominal_hash_rate=1.0e9, max_target_int=(2**256 - 1)))

            # Read frames until the channel is opened (SetTarget / early jobs
            # may arrive first; they are handled after connect by the listener,
            # but we consume any that precede the success reply here).
            opened = False
            for _ in range(20):
                mt, payload = transport.recv_message()
                if mt == SV2_MT_OPEN_STANDARD_MINING_CHANNEL_OK:
                    info = sv2_parse_open_standard_channel_success(payload)
                    self.sv2_channel_id = info["channel_id"]
                    self.sv2_extranonce = info["extranonce_prefix"]
                    self.sv2_target     = info["target"]
                    opened = True
                    break
                elif mt == SV2_MT_OPEN_MINING_CHANNEL_ERROR:
                    info = sv2_parse_open_channel_error(payload)
                    safe_print(f"[POOL:{self.name}] sv2 OpenChannel error: {info.get('error_code')}")
                    raise Sv2Error("OpenStandardMiningChannel rejected")
                elif mt == SV2_MT_SET_TARGET:
                    self.sv2_target = sv2_parse_set_target(payload)["maximum_target"]
                # Jobs (0x1e/0x20) arriving before success are re-requested by
                # the pool after channel open, so they are safely ignored here.
            if not opened:
                raise Sv2Error("no OpenStandardMiningChannel.Success received")

            transport.settimeout(300)
            self.sv2 = transport
            self.sock = s
            self.connected = True
            self.protocol = "sv2"
            self.sv2_detected = True
            safe_print(f"[POOL:{self.name}] Connected to {self.host}:{self.port} (sv2, channel={self.sv2_channel_id})")
            LOG.info(f"Connected {self.name} {self.host}:{self.port} protocol=sv2 channel={self.sv2_channel_id}")
            return True

        except Exception as e:
            LOG.warning(f"Pool {self.name}: sv2 negotiation failed ({e}); will try sv1")
            if s is not None:
                try: s.close()
                except: pass
            self.sv2 = None
            return False

    def submit_sv2(self, job_id, nonce_int) -> bool:
        """Submit a share over sv2 (SubmitSharesStandard). Returns success."""
        meta = self.sv2_submit_meta.get(job_id)
        if not meta or not self.sv2:
            return False
        submit_nonce = byteswap_u32(nonce_int)
        with self.lock:
            seq = self.sv2_seq
            self.sv2_seq += 1
        try:
            self.sv2.send_message(
                SV2_MT_SUBMIT_SHARES_STANDARD,
                sv2_msg_submit_shares_standard(
                    meta["channel_id"], seq, job_id,
                    submit_nonce, meta["ntime"], meta["version"]),
                channel_msg=True)
            return True
        except Exception as e:
            LOG.error(f"sv2 submit to {self.name}: {e}")
            return False

    def reconnect(self):
        safe_print(f"[POOL:{self.name}] Reconnecting...")
        try: self.sock.close()
        except: pass
        self.sock = None; self.connected = False; time.sleep(5)
        return self.connect()

    def set_difficulty(self, diff):
        with self.lock:
            self.pool_diff = diff
            self.share_target = int(DIFF1/diff)

    def submit(self, address, job_id, en2, ntime, nonce_hex):
        try:
            payload = json.dumps({
                "id": int(time.time()*1000)%100000+10,
                "method": "mining.submit",
                "params": [address, job_id, en2, ntime, nonce_hex]
            })+"\n"
            if self.sock:
                self.sock.sendall(payload.encode())
                return True
        except Exception as e:
            LOG.error(f"Submit to {self.name}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  MAIN MINER (Dual Pool)
# ═══════════════════════════════════════════════════════════════

class AISoloMinerV8:
    NUM_REGIONS = 32

    def __init__(self, address=BTC_ADDRESS, num_cores=None, use_gpu=False,
                 gpu_only=False, gpu_difficulty=0, bch_address=None, bch_pools=None,
                 tg_token="", tg_chat=""):
        self.address    = address
        self.bch_address = bch_address
        self.shutdown   = False
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.num_cores  = num_cores if num_cores else CPU_COUNT
        self.use_gpu    = use_gpu
        self.gpu_only   = gpu_only
        self.gpu_manual_diff = gpu_difficulty  # 0 = auto, >0 = fixed difficulty
        self.tg_token   = tg_token
        self.tg_chat    = tg_chat

        # Pool connections
        self.pools: List[PoolConnection] = []
        for i, p in enumerate(POOLS):
            pool = PoolConnection(i, p['name'], p['host'], p['port'], self.address,
                                  p.get('authkey'))
            self.pools.append(pool)

        # BCH pool connections
        self.bch_pools: List[PoolConnection] = []
        if bch_pools:
            for i, p in enumerate(bch_pools):
                pool = PoolConnection(100 + i, p['name'], p['host'], p['port'],
                                      self.bch_address or self.address, p.get('authkey'))
                self.bch_pools.append(pool)

        # Stats
        self.total_hashes:    int   = 0
        self.blocks_found:    int   = 0
        self.shares_accepted: Dict[str,int] = {p['name']:0 for p in POOLS}
        self.shares_rejected: Dict[str,int] = {p['name']:0 for p in POOLS}
        self.best_hash_int:   int   = 2**256-1
        self.best_share_diff: float = 0.0
        self.best_share_hex:  str   = "f"*64
        self.stats_lock = threading.Lock()

        # Per-core hashrates
        self.core_hashrates: Dict[int,float] = {}
        self.core_c_mode:    Dict[int,bool]  = {}

        # Per-pool hashrates
        self.pool_hashrates: Dict[str,float] = {p['name']:0.0 for p in POOLS}
        self.core_last_pool: Dict[int,str] = {}
        self.core_last_hr:   Dict[int,float] = {}
        self.last_submit_engine: Dict[str,str] = {}

        # Network
        self.net_diff=0.0; self.net_height=0; self.net_hr=0.0
        self.net_eta='?'; self.net_bua=0; self.last_net=0.0
        self.net_halving=0; self.net_halving_blocks=0; self.net_halving_str='?'

        # BCH Network
        self.bch_net_diff=0.0; self.bch_net_height=0; self.bch_net_hr=0.0
        self.bch_net_eta='?'; self.bch_net_bua=0; self.bch_last_net=0.0

        # Shared memory
        self.bandit_rewards = Array('d', [0.0]*self.NUM_REGIONS)
        self.bandit_counts  = Array('d', [1.0]*self.NUM_REGIONS)
        self.best_hash_val  = Value('d', float(2**256-1))
        self.shutdown_flag  = Value('b', 0)
        self.share_target_v = Value('d', 1.0)

        # GPU adaptive difficulty
        self.gpu_last_share_time = time.time()
        self.gpu_share_count = 0
        self.gpu_difficulty_reduce_interval = 3600  # 1 hour

        self.job_queues: List[Queue] = []
        self.result_queue = Queue(maxsize=1000)
        self.workers: List[Process] = []
        self.gpu_workers: List[Process] = []
        self.gpu_hashrate: float = 0.0

        # Hall of fame
        self.hall_of_fame = []
        self.hof_lock = threading.Lock()

        self.bloom_bits = np.zeros(500000, dtype=bool)

        # Vardiff
        self.last_vardiff   = 0.0
        self.share_times:   List[float] = []

        # Telegram
        self.telegram = TelegramAlert(self.tg_token, self.tg_chat)

        self._init_db()

        _, self.c_available = load_c_engine(self.script_dir)
        engine_str = "C Extension (midstate)" if self.c_available else "Python fallback"
        safe_print(f"[ENGINE] {engine_str}")

    def _init_db(self):
        try:
            self.db = sqlite3.connect('ai_miner_v8.db', check_same_thread=False)
            self.db_lock = threading.Lock()
            c = self.db.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS best_shares(id INTEGER PRIMARY KEY,"
                      "timestamp TEXT,diff REAL,size_str TEXT,hash_hex TEXT,nonce TEXT,pool TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,"
                      "start TEXT,hashes INTEGER,shares INTEGER,best_diff REAL,hashrate REAL)")
            self.db.commit()
        except: self.db=None; self.db_lock=threading.Lock()

    # ── POOL LISTENERS ────────────────────────────────────────

    def listen_for_jobs(self, pool: PoolConnection):
        buf=''; errors=0
        while not self.shutdown:
            try:
                if not pool.sock: time.sleep(1); continue
                pool.sock.settimeout(30)
                chunk = pool.sock.recv(8192).decode('utf-8', errors='ignore')
                if not chunk:
                    errors+=1
                    if errors>=3: pool.reconnect(); errors=0
                    continue
                buf+=chunk; lines=buf.split('\n'); buf=lines[-1]
                for line in lines[:-1]:
                    if not line.strip(): continue
                    try:
                        msg=json.loads(line)
                        LOG.info(f"[POOL:{pool.name}] {line.strip()[:120]}")
                        m=msg.get('method')
                        if m=='mining.notify':
                            self._dispatch(pool, msg['params'])
                        elif m=='mining.set_difficulty':
                            self._set_diff(pool, msg['params'])
                        elif m is None and 'result' in msg and msg.get('id') not in(1,2):
                            self._handle_response(pool, msg)
                    except json.JSONDecodeError: pass
                time.sleep(0.005)
            except socket.timeout: continue
            except Exception as e:
                LOG.error(f"Listener[{pool.name}]:{e}"); errors+=1
                if errors>=3: pool.reconnect(); errors=0
                time.sleep(1)

    def _set_diff(self, pool: PoolConnection, params):
        try:
            d = float(params[0])
            pool.set_difficulty(d)
            # Store 1/d so workers compute use_target = DIFF1 * (1/d) = DIFF1/d
            ratio = 1.0 / d if d > 0 else 1.0
            self.share_target_v.value = ratio
            target = int(DIFF1 / d) if d > 0 else DIFF1
            safe_print(f"[POOL:{pool.name}] Difficulty={d:.6f} target={target:#x}")
            LOG.info(f"Pool {pool.name} difficulty set to {d} (share_target_v={ratio})")
        except Exception as e: LOG.error(f"set_diff[{pool.name}]:{e}")

    def _dispatch(self, pool: PoolConnection, params):
        job = {
            'job_id':        params[0],
            'prevhash':      params[1],
            'coinb1':        params[2],
            'coinb2':        params[3],
            'merkle_branch': params[4],
            'version':       params[5],
            'nbits':         params[6],
            'ntime':         params[7],
            'clean_jobs':    params[8],
            '_target':       nbits_to_target(params[6]),
            '_en1':          pool.extranonce1,
            '_en2_size':     pool.extranonce2_size,
            '_pool_id':      pool.pool_id,
            '_pool_name':    pool.name,
        }
        pool.job_data = job
        safe_print(f"[JOB:{pool.name}] {params[0][:20]}... -> {len(self.workers)} cores")
        for q in self.job_queues:
            try: q.put_nowait(job)
            except: pass

    def _handle_response(self, pool: PoolConnection, msg):
        try:
            if msg.get('result') is True:
                with self.stats_lock:
                    self.shares_accepted[pool.name] += 1
                acc = self.shares_accepted[pool.name]
                self.share_times.append(time.time())
                if len(self.share_times)>20: self.share_times.pop(0)
                safe_print(f"[SHARE:{pool.name}] ACCEPTED! Total={acc}")
                LOG.info(f"Share ACCEPTED on {pool.name}")
                engine = self.last_submit_engine.get(pool.name, "CPU")
                self.telegram.notify_share_accepted(pool.name, acc, engine)
            else:
                with self.stats_lock:
                    self.shares_rejected[pool.name] += 1
                err = msg.get('error','unknown')
                safe_print(f"[SHARE:{pool.name}] REJECTED: {err}")
                LOG.warning(f"Share REJECTED on {pool.name}: {err}")
        except Exception as e: LOG.error(f"Response[{pool.name}]:{e}")

    # ── SV2 LISTENER / JOB PIPELINE ───────────────────────────

    def listen_for_jobs_sv2(self, pool: PoolConnection):
        """Read the encrypted sv2 stream, translate NewMiningJob +
        SetNewPrevHash into the same job dicts the sv1 pipeline feeds to the
        workers, and route SubmitShares.Success/Error into share stats."""
        errors = 0
        while not self.shutdown:
            tr = pool.sv2
            if tr is None:
                time.sleep(1); continue
            try:
                mt, payload = tr.recv_message()
                errors = 0
            except socket.timeout:
                continue
            except Exception as e:
                LOG.error(f"SV2Listener[{pool.name}]:{e}")
                errors += 1
                if errors >= 3:
                    pool.reconnect(); errors = 0
                time.sleep(1)
                continue
            try:
                if mt == SV2_MT_NEW_MINING_JOB:
                    job = sv2_parse_new_mining_job(payload)
                    pool.sv2_jobs_meta[job['job_id']] = job
                    LOG.info(f"[SV2:{pool.name}] NewMiningJob id={job['job_id']} future={job['min_ntime'] is None}")
                    if job['min_ntime'] is not None:
                        self._dispatch_sv2(pool, job, pool.sv2_prevhash)
                    elif pool.sv2_prevhash and pool.sv2_prevhash.get('job_id') == job['job_id']:
                        self._dispatch_sv2(pool, job, pool.sv2_prevhash)
                elif mt == SV2_MT_SET_NEW_PREV_HASH:
                    ph = sv2_parse_set_new_prev_hash(payload)
                    pool.sv2_prevhash = ph
                    LOG.info(f"[SV2:{pool.name}] SetNewPrevHash job={ph['job_id']}")
                    job = pool.sv2_jobs_meta.get(ph['job_id'])
                    if job:
                        self._dispatch_sv2(pool, job, ph)
                elif mt == SV2_MT_SET_TARGET:
                    info = sv2_parse_set_target(payload)
                    pool.sv2_target = info['maximum_target']
                    self._apply_sv2_target(pool)
                elif mt == SV2_MT_SUBMIT_SHARES_SUCCESS:
                    self._handle_sv2_success(pool, payload)
                elif mt == SV2_MT_SUBMIT_SHARES_ERROR:
                    self._handle_sv2_error(pool, payload)
                elif mt == SV2_MT_RECONNECT:
                    LOG.info(f"[SV2:{pool.name}] Reconnect requested")
                    pool.reconnect()
                # Other message types are safely ignored for standard channels.
            except Exception as e:
                LOG.error(f"SV2Dispatch[{pool.name}]:{e}")

    def _apply_sv2_target(self, pool: PoolConnection):
        """Translate the sv2 channel target into the shared worker share
        target ratio (same mechanism the sv1 path uses via set_difficulty)."""
        if pool.sv2_target and pool.sv2_target > 0:
            ratio = pool.sv2_target / DIFF1
            self.share_target_v.value = min(max(ratio, 1e-12), 1.0)
            pool.set_difficulty(max(DIFF1 / pool.sv2_target, 1e-12))
            LOG.info(f"[SV2:{pool.name}] target={pool.sv2_target:#x} diff={pool.pool_diff:.6f}")

    def _dispatch_sv2(self, pool: PoolConnection, job, prevhash):
        """Build a worker job dict from an sv2 standard job + prevhash and
        push it into every worker queue (mirrors _dispatch for sv1)."""
        if not job or not prevhash:
            return
        version  = job['version']
        merkle   = job['merkle_root']          # raw 32 bytes, header order
        prev_h   = prevhash['prev_hash']       # raw 32 bytes, header order
        ntime    = job['min_ntime'] if job['min_ntime'] is not None else prevhash['min_ntime']
        nbits    = prevhash['nbits']

        prefix = (version.to_bytes(4, 'little').hex()
                  + prev_h.hex()
                  + merkle.hex()
                  + ntime.to_bytes(4, 'little').hex()
                  + nbits.to_bytes(4, 'little').hex())
        if len(prefix) != 152:
            LOG.error(f"[SV2:{pool.name}] bad header prefix len={len(prefix)}")
            return

        wjob = {
            'job_id':        job['job_id'],
            'prevhash':      prev_h.hex(),
            'coinb1':        '', 'coinb2': '',
            'merkle_branch': [],
            'version':       version.to_bytes(4, 'little').hex(),
            'nbits':         nbits.to_bytes(4, 'little').hex(),
            'ntime':         ntime.to_bytes(4, 'little').hex(),
            'clean_jobs':    True,
            '_prebuilt_prefix': prefix,
            '_target':       nbits_int_to_target(nbits),   # block target
            '_en1':          '', '_en2_size': 0,
            '_pool_id':      pool.pool_id,
            '_pool_name':    pool.name,
            '_sv2':          True,
        }
        pool.sv2_submit_meta[job['job_id']] = {
            'version': version, 'ntime': ntime,
            'channel_id': pool.sv2_channel_id,
        }
        # Bound the per-pool job bookkeeping (keep the most recent entries).
        for _meta in (pool.sv2_jobs_meta, pool.sv2_submit_meta):
            if len(_meta) > 128:
                for _k in list(_meta.keys())[:-64]:
                    _meta.pop(_k, None)
        pool.job_data = wjob
        self._apply_sv2_target(pool)
        safe_print(f"[JOB:{pool.name}] sv2 job {job['job_id']} -> {len(self.job_queues)} workers")
        for q in self.job_queues:
            try: q.put_nowait(wjob)
            except: pass

    def _handle_sv2_success(self, pool: PoolConnection, payload):
        try:
            info = sv2_parse_submit_shares_success(payload)
            n = info.get('new_submits_accepted_count', 1) or 1
            with self.stats_lock:
                self.shares_accepted[pool.name] = self.shares_accepted.get(pool.name, 0) + n
                acc = self.shares_accepted[pool.name]
                self.share_times.append(time.time())
                if len(self.share_times) > 20: self.share_times.pop(0)
            safe_print(f"[SHARE:{pool.name}] sv2 ACCEPTED! Total={acc}")
            LOG.info(f"Share ACCEPTED on {pool.name} (sv2)")
            engine = self.last_submit_engine.get(pool.name, "CPU")
            self.telegram.notify_share_accepted(pool.name, acc, engine)
        except Exception as e:
            LOG.error(f"SV2Success[{pool.name}]:{e}")

    def _handle_sv2_error(self, pool: PoolConnection, payload):
        try:
            info = sv2_parse_submit_shares_error(payload)
            with self.stats_lock:
                self.shares_rejected[pool.name] = self.shares_rejected.get(pool.name, 0) + 1
            safe_print(f"[SHARE:{pool.name}] sv2 REJECTED: {info.get('error_code')}")
            LOG.warning(f"Share REJECTED on {pool.name} (sv2): {info.get('error_code')}")
        except Exception as e:
            LOG.error(f"SV2Error[{pool.name}]:{e}")

    def _find_pool(self, pool_id):
        for p in self.pools:
            if p.pool_id == pool_id:
                return p
        for p in self.bch_pools:
            if p.pool_id == pool_id:
                return p
        return self.pools[0] if self.pools else None

    # ── RESULT COLLECTOR ──────────────────────────────────────

    def collect_results(self):
        while not self.shutdown:
            try:
                msg = self.result_queue.get(timeout=1)
                t   = msg.get('type')

                if t=='solution':
                    self._submit(msg)

                elif t=='stats':
                    cid = msg['core_id']
                    pname = msg.get('_pool_name', 'unknown')
                    hr = msg['hashrate']
                    is_gpu = msg.get('_is_gpu', False)
                    with self.stats_lock:
                        self.total_hashes += msg['hashes']
                        self.core_hashrates[cid] = hr
                        self.core_c_mode[cid]    = msg['c_mode']
                        if is_gpu:
                            self.gpu_hashrate = hr
                        # Per-pool hashrate: subtract old, add new
                        old_pool = self.core_last_pool.get(cid)
                        old_hr   = self.core_last_hr.get(cid, 0.0)
                        if old_pool and old_pool in self.pool_hashrates:
                            self.pool_hashrates[old_pool] = max(0.0, self.pool_hashrates[old_pool] - old_hr)
                        if pname in self.pool_hashrates:
                            self.pool_hashrates[pname] += hr
                        self.core_last_pool[cid] = pname
                        self.core_last_hr[cid]   = hr
                        bi = msg['best_int']
                        if bi < self.best_hash_int:
                            self.best_hash_int  = bi
                            diff = int_to_diff(bi)
                            self.best_share_diff = diff
                            self.best_share_hex  = msg['best_hex']
                            pool_name = msg.get('_pool_name', 'unknown')
                            engine = "GPU" if cid >= 999 else "CPU"
                            safe_print(f"[BEST] {diff:.6f} diff ({fmt_share(diff)}) on {pool_name} [{engine}]")
                            self.telegram.notify_best_share(diff, fmt_share(diff), msg['best_hex'], pool_name, engine)
                            self._save_share(diff, fmt_share(diff), msg['best_hex'], '?', pool_name)

            except Exception: pass

    def _bloom_check_add(self, key: str) -> bool:
        positions = [int(hashlib.md5(f"{key}{i}".encode()).hexdigest(),16)%500000 for i in range(5)]
        if all(self.bloom_bits[p] for p in positions): return True
        for p in positions: self.bloom_bits[p]=True
        return False

    def _submit(self, msg):
        try:
            key = f"{msg['job_id']}{format(msg['nonce'],'08x')}"
            if self._bloom_check_add(key): return

            pool_id = msg.get('_pool_id', 0)
            pool = self._find_pool(pool_id) or self.pools[0]

            nonce_hex = format(msg['nonce'],'08x')
            is_gpu = msg.get('core_id', 0) >= 999
            engine = "GPU" if is_gpu else "CPU"
            if pool.protocol == "sv2" and pool.sv2 is not None:
                ok = pool.submit_sv2(msg['job_id'], msg['nonce'])
            else:
                ok = pool.submit(self.address, msg['job_id'], msg['en2'],
                               msg['ntime'], nonce_hex)
            if ok:
                with self.stats_lock:
                    self.last_submit_engine[pool.name] = engine
                safe_print(f"[SUBMIT:{pool.name}] {engine} Core {msg['core_id']} nonce={nonce_hex}")

                if is_gpu:
                    diff = int_to_diff(msg['best_int'])
                    pool_name = msg.get('_pool_name', pool.name)
                    self.gpu_last_share_time = time.time()
                    self.gpu_share_count += 1
                    self.telegram.send(
                        f"GPU Share Found!\n"
                        f"Pool: {pool_name}\n"
                        f"Engine: GPU\n"
                        f"Diff: {diff:.6f} ({fmt_share(diff)})\n"
                        f"Nonce: {nonce_hex}\n"
                        f"Hash: {msg['hash_hex'][:32]}...\n"
                        f"{time.strftime('%H:%M:%S')}"
                    )

            hi = msg['best_int']
            if hi <= pool.job_data.get('_target', 2**256-1):
                diff = int_to_diff(hi)
                safe_print(f"\n{'='*64}")
                safe_print(f"  *** BLOCK FOUND! {engine} Core {msg['core_id']} via {pool.name} ***")
                safe_print(f"  Hash:  {msg['hash_hex']}")
                safe_print(f"  Nonce: {nonce_hex}")
                safe_print(f"  Addr:  {self.address}")
                safe_print(f"{'='*64}\n")
                LOG.info(f"BLOCK FOUND on {pool.name}: {msg['hash_hex']}")
                with self.stats_lock: self.blocks_found+=1
                self.telegram.notify_block(msg['hash_hex'], nonce_hex, pool.name, engine)
        except Exception as e: LOG.error(f"Submit:{e}")

    # ── DB ────────────────────────────────────────────────────

    def _save_share(self, diff, size_str, hash_hex, nonce, pool_name):
        if not self.db: return
        def _do():
            with self.db_lock:
                try:
                    c=self.db.cursor()
                    c.execute("INSERT INTO best_shares(timestamp,diff,size_str,hash_hex,nonce,pool) VALUES(?,?,?,?,?,?)",
                              (time.strftime('%Y-%m-%d %H:%M:%S'),diff,size_str,hash_hex,nonce,pool_name))
                    c.execute("DELETE FROM best_shares WHERE id NOT IN(SELECT id FROM best_shares ORDER BY diff DESC LIMIT 50)")
                    self.db.commit()
                except: pass
        threading.Thread(target=_do, daemon=True).start()

    def _load_hof(self):
        if not self.db: return
        try:
            with self.db_lock:
                c=self.db.cursor()
                c.execute("SELECT timestamp,diff,size_str,hash_hex,nonce FROM best_shares ORDER BY diff DESC LIMIT 10")
                self.hall_of_fame=[{'time':r[0],'diff':r[1],'size':r[2],'hash':r[3],'nonce':r[4]} for r in c.fetchall()]
        except: pass

    # ── NETWORK ───────────────────────────────────────────────

    def update_network(self):
        try:
            h    = requests.get('https://blockchain.info/latestblock',timeout=10).json()['height']
            diff = float(requests.get('https://blockchain.info/q/getdifficulty',timeout=10).text)
            bua  = 2016-(h%2016)
            hrs,mins=divmod(bua*10,60)
            if hrs>24:
                d,hrs=divmod(hrs,24); eta=f"{d}d {hrs}h {mins}m"
            else: eta=f"{hrs}h {mins}m"
            halving_height = ((h // 210000) + 1) * 210000
            blocks_to_halving = halving_height - h
            halving_hrs = blocks_to_halving * 10 / 3600
            if halving_hrs > 24:
                halving_d, halving_hrs_rem = divmod(halving_hrs, 24)
                halving_str = f"{int(halving_d)}d {int(halving_hrs_rem)}h"
            else:
                halving_str = f"{int(halving_hrs)}h"
            self.net_diff=diff; self.net_height=h
            self.net_hr=diff*(2**32)/600; self.net_eta=eta; self.net_bua=bua
            self.net_halving=halving_height; self.net_halving_blocks=blocks_to_halving
            self.net_halving_str=halving_str
            self.last_net=time.time()
            LOG.info(f"Net: h={h} diff={diff:.2f}")
        except Exception as e: LOG.error(f"Network:{e}")

    def update_bch_network(self):
        try:
            h    = requests.get('https://bitcoincash.org/api/v1/getlatestblock',timeout=10).json()['height']
            diff = float(requests.get('https://bitcoincash.org/api/v1/getdifficulty',timeout=10).text)
            bua  = 2016-(h%2016)
            hrs,mins=divmod(bua*10,60)
            if hrs>24:
                d,hrs=divmod(hrs,24); eta=f"{d}d {hrs}h {mins}m"
            else: eta=f"{hrs}h {mins}m"
            self.bch_net_diff=diff; self.bch_net_height=h
            self.bch_net_hr=diff*(2**32)/600; self.bch_net_eta=eta; self.bch_net_bua=bua
            self.bch_last_net=time.time()
            LOG.info(f"BCH Net: h={h} diff={diff:.2f}")
        except Exception as e: LOG.error(f"BCH Network:{e}")

    # ── GPU ADAPTIVE DIFFICULTY ───────────────────────────────

    def adjust_gpu_difficulty(self):
        """Adaptive GPU difficulty: start easy, ramp up when shares found, drop when silent.
        If gpu_manual_diff > 0, use fixed difficulty instead."""
        if not self.gpu_workers:
            return

        # Manual difficulty override
        if self.gpu_manual_diff > 0:
            target = 1.0 / self.gpu_manual_diff
            if self.share_target_v.value != target:
                self.share_target_v.value = target
                safe_print(f"[GPU-ADAPT] Fixed difficulty: {self.gpu_manual_diff}")
            return

        now = time.time()
        time_since_share = now - self.gpu_last_share_time
        current_diff = self.share_target_v.value
        current_diff_num = 1.0 / current_diff if current_diff > 0 else 1.0

        # If no GPU share found in 1 hour, reduce difficulty (easier)
        if time_since_share > self.gpu_difficulty_reduce_interval:
            if current_diff < 1.0:
                # Double the difficulty number (halve the target = easier)
                new_diff_num = min(current_diff_num * 2, 1.0)
                new_target = 1.0 / new_diff_num
                self.share_target_v.value = new_target
                safe_print(f"[GPU-ADAPT] No share in {int(time_since_share/60)}min - reducing difficulty: {current_diff_num:.1f} -> {new_diff_num:.1f}")
                self.gpu_last_share_time = now  # Reset timer after reducing
                return

        # If GPU found shares recently, gradually increase difficulty (harder)
        if time_since_share < 60 and self.gpu_share_count > 0:
            # Found a share within the last minute — try doubling difficulty
            new_diff_num = min(current_diff_num * 2, 10000.0)
            new_target = 1.0 / new_diff_num
            self.share_target_v.value = new_target
            safe_print(f"[GPU-ADAPT] Shares coming in - increasing difficulty: {current_diff_num:.1f} -> {new_diff_num:.1f}")
            # Reset share counter after adjusting
            self.gpu_share_count = 0
            self.gpu_last_share_time = now

    def display_status(self):
        time.sleep(15)
        while not self.shutdown:
            try:
                # Adjust GPU difficulty periodically
                self.adjust_gpu_difficulty()

                elapsed = max(time.time()-self.start_time, 1)
                with self.stats_lock:
                    total_h  = self.total_hashes
                    blocks   = self.blocks_found
                    acc      = dict(self.shares_accepted)
                    rej      = dict(self.shares_rejected)
                    bdiff    = self.best_share_diff
                    bhex     = self.best_share_hex
                    core_hrs = dict(self.core_hashrates)
                    core_cm  = dict(self.core_c_mode)
                    pool_hrs = dict(self.pool_hashrates)

                total_hr = sum(core_hrs.values())

                if time.time()-self.last_net>60:
                    threading.Thread(target=self.update_network,daemon=True).start()

                engine_str = "C+" if self.c_available else "Py"
                gpu_str = f" + GPU" if self.gpu_workers else ""
                gpu_hr = self.gpu_hashrate

                # Build display
                lines = []
                lines.append("")
                lines.append("=" * 62)
                lines.append("     TrueCryptoMiner v9 - by Truescent")
                lines.append(f"  {self.address}")
                lines.append("=" * 62)
                lines.append(f"  Engine      : {engine_str}{gpu_str}")
                lines.append(f"  Workers     : {len(self.workers)} CPU" + (f" + 1 GPU" if self.gpu_workers else ""))
                lines.append(f"  Hashrate    : {fmt_hr(total_hr + gpu_hr)}")
                lines.append(f"  Total Hashes: {total_h:,}")
                lines.append(f"  Uptime      : {int(elapsed//3600)}h {int((elapsed%3600)//60)}m {int(elapsed%60)}s")
                lines.append("-" * 62)

                # Pools
                lines.append("  Pools:")
                for p in self.pools:
                    st = "ONLINE" if p.connected else "OFFLINE"
                    hr = pool_hrs.get(p.name, 0.0)
                    lines.append(f"    {p.name:8s} {st:8s} [{p.protocol}]  HR={fmt_hr(hr):>10s}  diff={p.pool_diff:.2f}  acc={acc.get(p.name,0)} rej={rej.get(p.name,0)}")

                # GPU
                if self.gpu_workers:
                    gpu_p = self.gpu_workers[0]
                    gpu_st = "RUNNING" if gpu_p.is_alive() else "DEAD"
                    lines.append(f"    GPU      {gpu_st:8s}  HR={fmt_hr(gpu_hr):>10s}")

                lines.append("-" * 62)

                # Per-core
                lines.append("  CPU Cores:")
                for cid in sorted(core_hrs.keys()):
                    if cid >= 900:
                        continue
                    hr = core_hrs[cid]
                    eng = "C+" if core_cm.get(cid) else "Py"
                    lines.append(f"    Core {cid:2d} [{eng}]: {fmt_hr(hr)}")
                lines.append("-" * 62)

                # Mining stats
                lines.append(f"  Best Share : {bdiff:.6f} ({fmt_share(bdiff)})")
                lines.append(f"  Blocks     : {blocks}")

                # Hall of fame
                with self.hof_lock:
                    hof = list(self.hall_of_fame[:5])
                if hof:
                    lines.append("  Hall of Fame:")
                    for i,r in enumerate(hof):
                        lines.append(f"    #{i+1}: {r['diff']:.6f} diff ({r['size']}) {r['time']}")
                lines.append("-" * 62)

                # Network
                lines.append(f"  Network Height  : {self.net_height:,}")
                lines.append(f"  Network HR      : {fmt_hr(self.net_hr)}")
                lines.append(f"  Difficulty      : {self.net_diff:,.0f}")
                lines.append(f"  Next Adj        : {self.net_eta} ({self.net_bua} blocks)")
                if self.net_halving > 0:
                    lines.append(f"  Next Halving    : Block {self.net_halving:,} ({self.net_halving_blocks:,} blocks / ~{self.net_halving_str})")
                lines.append(f"  Telegram        : Active")
                lines.append("=" * 62)
                lines.append("")

                if IS_WINDOWS:
                    os.system('cls')
                print('\n'.join(lines))

                # Auto-restart dead workers
                for i, p in enumerate(self.workers):
                    if not p.is_alive():
                        LOG.error(f"Worker {i} died -- restarting")
                        safe_print(f"[!] Core {i} process died -- restarting...")
                        new_p = Process(
                            target=worker_process,
                            args=(i, self.num_cores, self.script_dir,
                                  self.job_queues[i], self.result_queue,
                                  self.bandit_rewards, self.bandit_counts,
                                  self.best_hash_val, self.shutdown_flag,
                                  self.share_target_v),
                            daemon=True)
                        new_p.start()
                        self.workers[i] = new_p

                # Restart dead GPU workers
                for i, p in enumerate(self.gpu_workers):
                    if not p.is_alive():
                        LOG.error(f"GPU worker {i} died -- restarting")
                        safe_print(f"[!] GPU worker {i} died -- restarting...")
                        gpu_idx = len(self.workers) + i
                        new_p = Process(
                            target=gpu_worker_process,
                            args=(self.script_dir, self.job_queues[gpu_idx],
                                  self.result_queue, self.best_hash_val,
                                  self.shutdown_flag, self.share_target_v),
                            daemon=True)
                        new_p.start()
                        self.gpu_workers[i] = new_p

                time.sleep(60)
            except Exception as e:
                LOG.error(f"Status:{e}"); time.sleep(5)

    # ── START ─────────────────────────────────────────────────

    def start(self):
        btc_pool_names = " + ".join([f"{p.host}:{p.port}" for p in self.pools])
        bch_pool_names = " + ".join([f"{p.host}:{p.port}" for p in self.bch_pools]) if self.bch_pools else "None"
        gpu_str = " + CUDA GPU" if self.use_gpu else ""
        bch_str = f"\n  BCH: {self.bch_address or 'Not set'}\n  BCH Pools: {bch_pool_names}" if self.bch_pools else ""
        safe_print(f"""
===============================================================
       TrueMiner v1 - BTC + BCH Dual Mine
  BTC: {self.address}
  BTC Pools: {btc_pool_names}{bch_str}
  CPU + C Extension{gpu_str}
  Cores: {self.num_cores}
===============================================================
        """)

        optimise_system()

        safe_print("[INIT] Fetching network info...")
        self.update_network()
        if self.bch_pools:
            threading.Thread(target=self.update_bch_network, daemon=True).start()

        safe_print("[INIT] Loading hall of fame...")
        self._load_hof()

        # Connect to BTC pools
        connected_pools = 0
        for pool in self.pools:
            safe_print(f"[INIT] Connecting to {pool.name} ({pool.host}:{pool.port})...")
            if pool.connect():
                connected_pools += 1
            else:
                safe_print(f"[WARN] Could not connect to {pool.name}")

        # Connect to BCH pools
        connected_bch = 0
        for pool in self.bch_pools:
            safe_print(f"[INIT] Connecting to BCH pool {pool.name} ({pool.host}:{pool.port})...")
            if pool.connect():
                connected_bch += 1
            else:
                safe_print(f"[WARN] Could not connect to BCH pool {pool.name}")

        total_connected = connected_pools + connected_bch
        if total_connected == 0:
            safe_print("[ERROR] Cannot connect to any pool. Exiting.")
            return

        safe_print(f"[INIT] Connected: {connected_pools} BTC + {connected_bch} BCH pools")

        # Start listener threads for each pool
        all_pools = self.pools + self.bch_pools
        for pool in all_pools:
            if pool.connected:
                target = (self.listen_for_jobs_sv2
                          if pool.protocol == "sv2" else self.listen_for_jobs)
                threading.Thread(
                    target=target,
                    args=(pool,),
                    daemon=True
                ).start()

        threading.Thread(target=self.collect_results, daemon=True).start()

        # Wait for first job
        safe_print("[INIT] Waiting for first job...")
        for _ in range(90):
            if any(p.job_data for p in self.pools if p.connected): break
            time.sleep(1)
        if not any(p.job_data for p in self.pools if p.connected):
            safe_print("[ERROR] No job received. Exiting."); return

        self.start_time = time.time()

        # Spawn workers - split between BTC and BCH if both are available
        btc_cores = self.num_cores
        bch_cores = 0
        if self.bch_pools and connected_bch > 0:
            btc_cores = self.num_cores // 2
            bch_cores = self.num_cores - btc_cores
            safe_print(f"[INIT] Splitting workers: {btc_cores} BTC + {bch_cores} BCH")

        if self.gpu_only:
            safe_print("[INIT] GPU-only mode - skipping CPU workers")
        else:
            safe_print(f"[INIT] Spawning {self.num_cores} CPU worker processes...")
            
            # BTC workers
            for i in range(btc_cores):
                q = Queue(maxsize=8)
                self.job_queues.append(q)
                for p in self.pools:
                    if p.job_data:
                        try: q.put_nowait(p.job_data)
                        except: pass
                        break

                p = Process(
                    target=worker_process,
                    args=(i, self.num_cores, self.script_dir,
                          q, self.result_queue,
                          self.bandit_rewards, self.bandit_counts,
                          self.best_hash_val, self.shutdown_flag,
                          self.share_target_v),
                    daemon=True
                )
                p.start()
                self.workers.append(p)
                safe_print(f"[INIT] BTC Core {i} started (PID {p.pid})")

            # BCH workers
            bch_job_queues = []
            bch_workers = []
            for i in range(bch_cores):
                q = Queue(maxsize=8)
                bch_job_queues.append(q)
                self.job_queues.append(q)
                for p in self.bch_pools:
                    if p.job_data:
                        try: q.put_nowait(p.job_data)
                        except: pass
                        break

                p = Process(
                    target=worker_process,
                    args=(btc_cores + i, self.num_cores, self.script_dir,
                          q, self.result_queue,
                          self.bandit_rewards, self.bandit_counts,
                          self.best_hash_val, self.shutdown_flag,
                          self.share_target_v),
                    daemon=True
                )
                p.start()
                self.workers.append(p)
                bch_workers.append(p)
                safe_print(f"[INIT] BCH Core {btc_cores + i} started (PID {p.pid})")

        safe_print(f"[RUNNING] {self.num_cores} CPU processes mining on {connected_pools} pools. Ctrl+C to stop.\n")
        self.telegram.notify_startup(self.num_cores, self.c_available, self.pools)

        # Spawn GPU worker if requested - alternate between BTC and BCH
        if self.use_gpu:
            # BTC GPU worker
            gpu_q = Queue(maxsize=8)
            for p in self.pools:
                if p.job_data:
                    try: gpu_q.put_nowait(p.job_data)
                    except: pass
                    break
            self.job_queues.append(gpu_q)

            gpu_p = Process(
                target=gpu_worker_process,
                args=(self.script_dir, gpu_q, self.result_queue,
                      self.best_hash_val, self.shutdown_flag,
                      self.share_target_v),
                daemon=True
            )
            gpu_p.start()
            self.gpu_workers.append(gpu_p)
            safe_print(f"[INIT] BTC GPU worker started (PID {gpu_p.pid})")

            # BCH GPU worker
            if self.bch_pools and connected_bch > 0:
                gpu_bch_q = Queue(maxsize=8)
                for p in self.bch_pools:
                    if p.job_data:
                        try: gpu_bch_q.put_nowait(p.job_data)
                        except: pass
                        break
                self.job_queues.append(gpu_bch_q)

                gpu_bch_p = Process(
                    target=gpu_worker_process,
                    args=(self.script_dir, gpu_bch_q, self.result_queue,
                          self.best_hash_val, self.shutdown_flag,
                          self.share_target_v),
                    daemon=True
                )
                gpu_bch_p.start()
                self.gpu_workers.append(gpu_bch_p)
                safe_print(f"[INIT] BCH GPU worker started (PID {gpu_bch_p.pid})")

        threading.Thread(target=self.display_status, daemon=True).start()

        try:
            while not self.shutdown:
                time.sleep(1)
        except KeyboardInterrupt:
            safe_print("\n[STOP] Shutting down all processes...")
            self.shutdown_flag.value = 1
            self.shutdown = True
            for p in self.workers:
                p.terminate()
                p.join(timeout=3)
            for p in self.gpu_workers:
                p.terminate()
                p.join(timeout=3)
            if self.db:
                try:
                    with self.db_lock:
                        c=self.db.cursor()
                        total_hr=sum(self.core_hashrates.values())
                        c.execute("INSERT INTO sessions(start,hashes,shares,best_diff,hashrate) VALUES(?,?,?,?,?)",
                                  (time.strftime('%Y-%m-%d %H:%M:%S'),
                                   self.total_hashes, sum(self.shares_accepted.values()),
                                   self.best_share_diff, total_hr))
                        self.db.commit()
                except: pass
            safe_print("[DONE] Session saved. Goodbye.")


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

DEFAULT_POOLS = [
    {"name": "ckpool",  "host": "solo.ckpool.org",         "port": 3333},
    {"name": "braiins", "host": "solo.stratum.braiins.com", "port": 3333},
]

KNOWN_POOLS = {
    "1": {"name": "ckpool",  "host": "solo.ckpool.org",         "port": 3333, "label": "ckpool (solo.ckpool.org:3333) [sv1]"},
    "2": {"name": "braiins", "host": "solo.stratum.braiins.com", "port": 3333, "label": "braiins (solo.stratum.braiins.com:3333) [sv1]"},
    "3": {"name": "publicpool", "host": "public-pool.io",       "port": 3333, "label": "publicpool (public-pool.io:3333) [sv1]"},
    "4": {"name": "minerpool",  "host": "solo.minerpool.com",   "port": 3333, "label": "minerpool (solo.minerpool.com:3333) [sv1]"},
    "5": {"name": "mkpool-bc2", "host": "stratum2+tcp://bc2.mkpool.com", "port": 3360,
          "authkey": MKPOOL_BC2_AUTHORITY_KEY,
          "label": "mkpool BitcoinII/bc2 (bc2.mkpool.com:3360) [sv2]"},
}


def parse_pool_arg(s):
    """Parse a pool spec 'scheme://host:port/AUTHKEY' (any part optional) into
    a pool dict with name/host/port/authkey. The scheme is preserved on the
    host so PoolConnection auto-negotiates sv1 vs sv2."""
    s = (s or "").strip()
    scheme = ""
    low = s.lower()
    for sch in SV2_SCHEMES + SV1_SCHEMES:
        if low.startswith(sch):
            scheme = s[:len(sch)]
            s = s[len(sch):]
            break
    authkey = None
    if "/" in s:
        s, _, path = s.partition("/")
        if path:
            authkey = path
    if ":" in s:
        host, _, port_str = s.rpartition(":")
        try: port = int(port_str)
        except ValueError: host, port = s, 3333
    else:
        host, port = s, 3333
    name = (host.split(".")[0] or "pool")
    return {"name": name, "host": scheme + host, "port": port, "authkey": authkey}

KNOWN_BCH_POOLS = {
    "1": {"name": "solopool", "host": "bch.solopool.eu", "port": 3333, "label": "solopool (bch.solopool.eu:3333)"},
}


def _prompt_one_pool(known_pools, default_pool, index):
    """Prompt the user to pick a single pool from `known_pools` or enter a
    custom one, returning a pool dict {name, host, port, authkey}.

    `index` (0-based) chooses which known-pool key is offered as the default so
    dual setups suggest a different pool per slot. Each pool independently
    supports SV1 or SV2: a custom sv2 host (stratum2+tcp:// prefix) additionally
    prompts for the pool's SV2 authority key."""
    keys = list(known_pools.keys())
    default_key = keys[index] if index < len(keys) else keys[0]
    choice = input(f"    Select pool [{default_key}]: ").strip()
    if not choice:
        choice = default_key

    if choice.upper() in ("C", "CUSTOM"):
        name = input("    Pool name: ").strip() or f"custom{index+1}"
        print("    Pool host (add stratum2+tcp:// prefix for Stratum V2)")
        host = input("    Pool host: ").strip()
        port_str = input("    Pool port [3333]: ").strip()
        port = int(port_str) if port_str else 3333
        # For sv2 pools, offer to capture the pool's authority key.
        _, hint = parse_pool_scheme(host)
        authkey = None
        if hint == "sv2" or "mkpool" in host.lower():
            default_ak = MKPOOL_BC2_AUTHORITY_KEY if "mkpool" in host.lower() else ""
            prompt = f"    SV2 authority key [{default_ak or 'none'}]: "
            ak = input(prompt).strip() or default_ak
            authkey = ak or None
        return {"name": name, "host": host, "port": port, "authkey": authkey}
    elif choice in known_pools:
        p = known_pools[choice]
        print(f"    -> {p['label']}")
        return {"name": p["name"], "host": p["host"], "port": p["port"],
                "authkey": p.get("authkey")}
    else:
        print(f"    Invalid choice, using {default_pool['name']}")
        return dict(default_pool)


def _prompt_pool_set(coin_label, known_pools, default_pools):
    """Prompt for one or two pools for a coin.

    Offers a single vs dual choice; when dual is selected the wizard explicitly
    asks for the FIRST pool (primary) and then the SECOND pool (backup), storing
    them in order so index 0 is the primary and index 1 is the failover/backup.
    Returns a list of pool dicts."""
    print()
    print(f"  Available {coin_label} pools:")
    for k, v in known_pools.items():
        print(f"    [{k}] {v['label']}")
    print(f"    [C] Custom pool")
    print()

    pool_count = input("  Use (1) one pool or (2) dual pools (primary + backup)? [1/2]: ").strip()
    num_pools = 2 if pool_count == "2" else 1

    pools = []
    if num_pools == 1:
        print()
        pools.append(_prompt_one_pool(known_pools, default_pools[0], 0))
    else:
        print()
        print("  Dual pools selected: enter your FIRST pool, then your SECOND pool.")
        print()
        print("  --- Pool #1 (primary) ---")
        pools.append(_prompt_one_pool(known_pools, default_pools[0], 0))
        print()
        print("  --- Pool #2 (backup) ---")
        backup_default = default_pools[1] if len(default_pools) > 1 else default_pools[0]
        pools.append(_prompt_one_pool(known_pools, backup_default, 1))
    return pools


def interactive_setup():
    """Prompt user for configuration when no CLI args are given."""
    global POOLS

    print()
    print("=" * 60)
    print("       TrueCryptoMiner v9 - by Truescent")
    print("       Setup Wizard")
    print("=" * 60)
    print()

    # Coin selection
    print("  Which coin(s) do you want to mine?")
    print("    [1] Bitcoin (BTC)")
    print("    [2] Bitcoin Cash (BCH)")
    print("    [3] Both")
    coin_choice = input("  Select coin(s) [1]: ").strip()
    if not coin_choice:
        coin_choice = "1"
    if coin_choice == "2":
        mine_btc = False
        mine_bch = True
        print("  -> Mining Bitcoin Cash (BCH)")
    elif coin_choice == "3":
        mine_btc = True
        mine_bch = True
        print("  -> Mining both Bitcoin (BTC) and Bitcoin Cash (BCH)")
    else:
        mine_btc = True
        mine_bch = False
        print("  -> Mining Bitcoin (BTC)")

    # BTC Address + pool selection (only when mining Bitcoin)
    addr = BTC_ADDRESS
    selected_pools = []
    if mine_btc:
        print()
        addr = input(f"  Bitcoin address [{BTC_ADDRESS}]: ").strip()
        if not addr:
            addr = BTC_ADDRESS
            print(f"  Using default: {addr}")

        # Pool selection (single or dual: primary + backup)
        selected_pools = _prompt_pool_set("BTC", KNOWN_POOLS, DEFAULT_POOLS)

    # GPU
    print()
    gpu_choice = input("  Enable GPU mining? (y/n) [n]: ").strip().lower()
    use_gpu = gpu_choice in ("y", "yes")

    gpu_difficulty = 0
    if use_gpu:
        print()
        print("  GPU Difficulty:")
        print("    [0] Auto (starts easy, adapts based on share rate)")
        print("    [1] Fixed difficulty 1 (easiest)")
        print("    [4] Fixed difficulty 4")
        print("    [8] Fixed difficulty 8")
        gpu_diff_input = input("  Select GPU difficulty [0]: ").strip()
        if gpu_diff_input and gpu_diff_input.isdigit():
            gpu_difficulty = int(gpu_diff_input)
            if gpu_difficulty > 0:
                print(f"  GPU fixed at difficulty {gpu_difficulty}")
            else:
                print("  GPU auto-adaptive difficulty enabled")
        else:
            print("  GPU auto-adaptive difficulty enabled")

    # BCH (only when mining Bitcoin Cash)
    use_bch = mine_bch
    bch_address = None
    bch_pools = []
    if use_bch:
        print()
        bch_addr = input("  Bitcoin Cash address: ").strip()
        if not bch_addr:
            print("  BCH address is required for BCH mining.")
            use_bch = False
        else:
            bch_address = bch_addr

            # BCH pool selection (single or dual: primary + backup)
            bch_pools = _prompt_pool_set("BCH", KNOWN_BCH_POOLS, BCH_POOLS)

    # Telegram notifications
    print()
    tg_choice = input("  Enable Telegram notifications? (y/n) [n]: ").strip().lower()
    use_tg = tg_choice in ("y", "yes")

    tg_token = ""
    tg_chat = ""
    if use_tg:
        tg_token = input("  Telegram Bot Token: ").strip()
        tg_chat = input("  Telegram Chat ID: ").strip()
        if tg_token and tg_chat:
            print("  Telegram notifications enabled")
        else:
            print("  Telegram disabled (missing token or chat ID)")
            use_tg = False
            tg_token = ""
            tg_chat = ""

    # Cores
    avail = mp.cpu_count()
    cores_str = input(f"  CPU cores to use [auto={max(1,avail-1)}]: ").strip()
    if cores_str:
        try:
            cores = int(cores_str)
            cores = max(1, min(cores, avail))
        except ValueError:
            cores = max(1, avail - 1)
    else:
        cores = max(1, avail - 1)

    print()
    print("-" * 60)
    coins_str = " + ".join([c for c, on in (("BTC", mine_btc), ("BCH", use_bch)) if on]) or "None"
    print(f"  Coins   : {coins_str}")
    if mine_btc:
        print(f"  Address : {addr}")
        pool_str = " + ".join([f"{p['host']}:{p['port']}" for p in selected_pools])
        print(f"  Pools   : {pool_str}")
    print(f"  GPU     : {'Enabled' if use_gpu else 'Disabled'}")
    if use_gpu:
        if gpu_difficulty > 0:
            print(f"  GPU Diff: {gpu_difficulty} (fixed)")
        else:
            print(f"  GPU Diff: Auto-adaptive")
    print(f"  BCH     : {'Enabled' if use_bch else 'Disabled'}")
    if use_bch:
        print(f"  BCH Addr: {bch_address}")
        bch_pool_str = " + ".join([f"{p['host']}:{p['port']}" for p in bch_pools]) if bch_pools else "None"
        print(f"  BCH Pool: {bch_pool_str}")
    print(f"  Telegram: {'Enabled' if tg_token and tg_chat else 'Disabled'}")
    print(f"  Cores   : {cores}/{avail}")
    print("-" * 60)

    confirm = input("\n  Start mining? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        sys.exit(0)

    return addr, selected_pools, use_gpu, cores, gpu_difficulty, bch_address if use_bch else None, bch_pools if use_bch else [], tg_token, tg_chat


def main():
    mp.freeze_support()
    global CPU_COUNT, POOLS

    import argparse
    parser = argparse.ArgumentParser(description='TrueCryptoMiner v9 - by Truescent')
    parser.add_argument('address', nargs='?', default=None, help='Bitcoin address')
    parser.add_argument('--cores', default=None, type=int, help='CPU cores to use')
    parser.add_argument('--gpu', action='store_true', help='Enable CUDA GPU mining')
    parser.add_argument('--gpu-only', action='store_true', help='GPU only, skip CPU workers')
    parser.add_argument('--bch', nargs='?', const=True, default=None, help='Enable BCH dual mining (optionally pass BCH address)')
    parser.add_argument('--bch-addr', default=None, help='Bitcoin Cash address for BCH mining')
    parser.add_argument('--gpu-diff', type=float, default=0, help='GPU difficulty (0=auto adaptive, 1-10000=manual fixed)')
    parser.add_argument('--tg-token', default=None, help='Telegram bot token')
    parser.add_argument('--tg-chat', default=None, help='Telegram chat ID')
    parser.add_argument('--pool1', default=None, help='Pool 1 host:port (e.g. solo.ckpool.org:3333)')
    parser.add_argument('--pool2', default=None, help='Pool 2 host:port (e.g. solo.stratum.braiins.com:3333)')
    parser.add_argument('--bch-pool', default=None, help='BCH pool host:port (e.g. solo.bchpool.org:3333)')
    parser.add_argument('--no-prompt', action='store_true', help='Skip interactive setup, use defaults')
    parser.add_argument('--selftest', action='store_true', help='Run sv2 codec/handshake self-checks and exit')
    args = parser.parse_args()

    if args.selftest:
        sys.exit(0 if sv2_selftest() else 1)

    # gpu_only is only ever set via CLI; define it up front so the interactive
    # path doesn't hit an UnboundLocalError when it is passed to the miner.
    gpu_only = args.gpu_only

    # Interactive setup if no address and not --no-prompt
    if args.address is None and not args.no_prompt:
        try:
            addr, selected_pools, use_gpu, cores, gpu_difficulty, bch_address, bch_pools, tg_token, tg_chat = interactive_setup()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return
    else:
        addr = args.address or BTC_ADDRESS
        use_gpu = args.gpu
        gpu_only = args.gpu_only
        gpu_difficulty = args.gpu_diff
        tg_token = args.tg_token or ""
        tg_chat = args.tg_chat or ""
        cores = None
        bch_address = None
        bch_pools = []

        # Parse pools from CLI (supports sv2 URLs, e.g.
        # "stratum2+tcp://bc2.mkpool.com:3360/AUTHKEY").
        selected_pools = []
        for pool_arg in [args.pool1, args.pool2]:
            if pool_arg:
                selected_pools.append(parse_pool_arg(pool_arg))

        if not selected_pools:
            selected_pools = list(DEFAULT_POOLS)

        # Parse BCH from CLI
        bch_address = None
        bch_pools = []
        if args.bch:
            if isinstance(args.bch, str):
                bch_address = args.bch
            elif args.bch_addr:
                bch_address = args.bch_addr
            else:
                bch_address = BCH_ADDRESS
                print("[INIT] BCH enabled with default address. Use --bch-addr to set your address.")
            if args.bch_pool:
                bch_pools.append(parse_pool_arg(args.bch_pool))
            else:
                bch_pools = [{"name": "solopool", "host": "bch.solopool.eu", "port": 3333}]

    # Apply pool config
    POOLS.clear()
    for p in selected_pools:
        POOLS.append(p)

    # Apply BCH pool config
    if bch_pools:
        BCH_POOLS.clear()
        for p in bch_pools:
            BCH_POOLS.append(p)

    # Apply core config
    if cores is not None:
        if cores < 1:
            safe_print(f"[!] Cores must be at least 1. Using 1.")
            CPU_COUNT = 1
        elif cores > mp.cpu_count():
            safe_print(f"[!] Only {mp.cpu_count()} cores available. Using {mp.cpu_count()}.")
            CPU_COUNT = mp.cpu_count()
        else:
            CPU_COUNT = cores
    elif args.cores is not None:
        if args.cores < 1:
            CPU_COUNT = 1
        elif args.cores > mp.cpu_count():
            CPU_COUNT = mp.cpu_count()
        else:
            CPU_COUNT = args.cores
    else:
        CPU_COUNT = max(1, mp.cpu_count() - 1)

    safe_print(f"[CORES] Using {CPU_COUNT} of {mp.cpu_count()} available cores")

    AISoloMinerV8(addr, CPU_COUNT, use_gpu=use_gpu, gpu_only=gpu_only,
                  gpu_difficulty=gpu_difficulty,
                  bch_address=bch_address,
                  bch_pools=bch_pools,
                  tg_token=tg_token,
                  tg_chat=tg_chat).start()

if __name__ == "__main__":
    main()
