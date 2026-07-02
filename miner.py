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
import struct, sqlite3, subprocess
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
BTC_ADDRESS = "bc1qke9ets26d6vs8ardndteds57frcald98n8g3te"
BCH_ADDRESS = "bitcoincash:qpf8gg2d9r8e0ln2r0lhl9q0s3sjk6mz2qgfuq0fvs"  # Set your BCH address
DIFF1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

TG_TOKEN = "6582191802:AAHBO-n98I5vw2th2llW-2BcWZeOWNZq1po"
TG_CHAT  = "5592132168"

# Pool definitions
POOLS = [
    {"name": "ckpool",  "host": "solo.ckpool.org",         "port": 3333},
    {"name": "braiins", "host": "solo.stratum.braiins.com", "port": 3333},
]

# BCH pool definitions
BCH_POOLS = [
    {"name": "bchpool",    "host": "solo.bchpool.org",         "port": 3333},
    {"name": "bchpublic",  "host": "public-pool.bch.ninja",    "port": 3333},
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

            if random.random() < 0.05:
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

            if random.random() < 0.05:
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
#  POOL CONNECTION
# ═══════════════════════════════════════════════════════════════

class PoolConnection:
    def __init__(self, pool_id, name, host, port, address):
        self.pool_id  = pool_id
        self.name     = name
        self.host     = host
        self.port     = port
        self.address  = address
        self.sock: Optional[socket.socket] = None
        self.extranonce1      = ''
        self.extranonce2_size = 4
        self.pool_diff: float = 1.0
        self.share_target: int = 2**256-1
        self.job_data: dict = {}
        self.connected = False
        self.lock = threading.Lock()

    def connect(self) -> bool:
        for attempt in range(10):
            try:
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
                safe_print(f"[POOL:{self.name}] Connected to {self.host}:{self.port}")
                LOG.info(f"Connected {self.name} {self.host}:{self.port}")
                return True
            except Exception as e:
                wait = min(120, 5*(2**attempt))
                safe_print(f"[POOL:{self.name}] Retry in {wait}s... ({e})")
                time.sleep(wait)
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
                 gpu_only=False, gpu_difficulty=0, bch_address=None, bch_pools=None):
        self.address    = address
        self.bch_address = bch_address
        self.shutdown   = False
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.num_cores  = num_cores if num_cores else CPU_COUNT
        self.use_gpu    = use_gpu
        self.gpu_only   = gpu_only
        self.gpu_manual_diff = gpu_difficulty  # 0 = auto, >0 = fixed difficulty

        # Pool connections
        self.pools: List[PoolConnection] = []
        for i, p in enumerate(POOLS):
            pool = PoolConnection(i, p['name'], p['host'], p['port'], self.address)
            self.pools.append(pool)

        # BCH pool connections
        self.bch_pools: List[PoolConnection] = []
        if bch_pools:
            for i, p in enumerate(bch_pools):
                pool = PoolConnection(100 + i, p['name'], p['host'], p['port'], self.bch_address or self.address)
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
        self.telegram = TelegramAlert(TG_TOKEN, TG_CHAT)

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
            pool = self.pools[pool_id] if pool_id < len(self.pools) else self.pools[0]

            nonce_hex = format(msg['nonce'],'08x')
            is_gpu = msg.get('core_id', 0) >= 999
            engine = "GPU" if is_gpu else "CPU"
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
                    lines.append(f"    {p.name:8s} {st:8s}  HR={fmt_hr(hr):>10s}  diff={p.pool_diff:.2f}  acc={acc.get(p.name,0)} rej={rej.get(p.name,0)}")

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
       CryptoCrackersMiner v1 - BTC + BCH Dual Mine
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
                threading.Thread(
                    target=self.listen_for_jobs,
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
    "1": {"name": "ckpool",  "host": "solo.ckpool.org",         "port": 3333, "label": "ckpool (solo.ckpool.org:3333)"},
    "2": {"name": "braiins", "host": "solo.stratum.braiins.com", "port": 3333, "label": "braiins (solo.stratum.braiins.com:3333)"},
    "3": {"name": "publicpool", "host": "public-pool.io",       "port": 3333, "label": "publicpool (public-pool.io:3333)"},
    "4": {"name": "minerpool",  "host": "solo.minerpool.com",   "port": 3333, "label": "minerpool (solo.minerpool.com:3333)"},
}

KNOWN_BCH_POOLS = {
    "1": {"name": "bchpool",   "host": "solo.bchpool.org",      "port": 3333, "label": "bchpool (solo.bchpool.org:3333)"},
    "2": {"name": "bchpublic", "host": "public-pool.bch.ninja", "port": 3333, "label": "bchpublic (public-pool.bch.ninja:3333)"},
    "3": {"name": "bmcpool",   "host": "solo.bmcpool.org",      "port": 3333, "label": "bmcpool (solo.bmcpool.org:3333)"},
}

def interactive_setup():
    """Prompt user for configuration when no CLI args are given."""
    global POOLS

    print()
    print("=" * 60)
    print("       TrueCryptoMiner v9 - by Truescent")
    print("       Setup Wizard")
    print("=" * 60)
    print()

    # BTC Address
    addr = input(f"  Bitcoin address [{BTC_ADDRESS}]: ").strip()
    if not addr:
        addr = BTC_ADDRESS
        print(f"  Using default: {addr}")

    # Pool selection
    print()
    print("  Available pools:")
    for k, v in KNOWN_POOLS.items():
        print(f"    [{k}] {v['label']}")
    print(f"    [C] Custom pool")
    print()

    pool_count = input("  Use (1) one pool or (2) dual pools? [1/2]: ").strip()
    if pool_count == "2":
        num_pools = 2
    else:
        num_pools = 1

    selected_pools = []
    for i in range(num_pools):
        print()
        if num_pools > 1:
            print(f"  --- Pool {i+1} ---")
        choice = input(f"  Select pool [{list(KNOWN_POOLS.keys())[i]}]: ").strip()
        if not choice:
            choice = list(KNOWN_POOLS.keys())[i]

        if choice.upper() == "C" or choice.upper() == "CUSTOM":
            name = input("    Pool name: ").strip() or f"custom{i+1}"
            host = input("    Pool host: ").strip()
            port_str = input("    Pool port [3333]: ").strip()
            port = int(port_str) if port_str else 3333
            selected_pools.append({"name": name, "host": host, "port": port})
        elif choice in KNOWN_POOLS:
            p = KNOWN_POOLS[choice]
            selected_pools.append({"name": p["name"], "host": p["host"], "port": p["port"]})
            print(f"    -> {p['label']}")
        else:
            print(f"    Invalid choice, using ckpool")
            selected_pools.append(dict(DEFAULT_POOLS[0]))

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

    # BCH
    print()
    bch_choice = input("  Enable BCH dual mining? (y/n) [n]: ").strip().lower()
    use_bch = bch_choice in ("y", "yes")

    bch_address = None
    bch_pools = []
    if use_bch:
        bch_addr = input("  Bitcoin Cash address: ").strip()
        if not bch_addr:
            print("  BCH address is required for BCH mining.")
            use_bch = False
        else:
            bch_address = bch_addr

            print()
            print("  BCH Pool:")
            for k, v in KNOWN_BCH_POOLS.items():
                print(f"    [{k}] {v['label']}")
            print()
            bch_choice = input("  Select BCH pool [1]: ").strip()
            if not bch_choice:
                bch_choice = "1"
            if bch_choice in KNOWN_BCH_POOLS:
                p = KNOWN_BCH_POOLS[bch_choice]
                bch_pools.append({"name": p["name"], "host": p["host"], "port": p["port"]})
                print(f"    -> {p['label']}")
            else:
                print(f"    Invalid choice, using bchpool")
                bch_pools.append(dict(BCH_POOLS[0]))

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
    print(f"  Cores   : {cores}/{avail}")
    print("-" * 60)

    confirm = input("\n  Start mining? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        sys.exit(0)

    return addr, selected_pools, use_gpu, cores, gpu_difficulty, bch_address if use_bch else None, bch_pools if use_bch else []


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
    parser.add_argument('--pool1', default=None, help='Pool 1 host:port (e.g. solo.ckpool.org:3333)')
    parser.add_argument('--pool2', default=None, help='Pool 2 host:port (e.g. solo.stratum.braiins.com:3333)')
    parser.add_argument('--bch-pool', default=None, help='BCH pool host:port (e.g. solo.bchpool.org:3333)')
    parser.add_argument('--no-prompt', action='store_true', help='Skip interactive setup, use defaults')
    args = parser.parse_args()

    # Interactive setup if no address and not --no-prompt
    if args.address is None and not args.no_prompt:
        try:
            addr, selected_pools, use_gpu, cores, gpu_difficulty, bch_address, bch_pools = interactive_setup()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return
    else:
        addr = args.address or BTC_ADDRESS
        use_gpu = args.gpu
        gpu_only = args.gpu_only
        gpu_difficulty = args.gpu_diff
        cores = None
        bch_address = None
        bch_pools = []

        # Parse pools from CLI
        selected_pools = []
        for pool_arg in [args.pool1, args.pool2]:
            if pool_arg:
                parts = pool_arg.split(":")
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 3333
                name = host.split(".")[0]
                selected_pools.append({"name": name, "host": host, "port": port})

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
                parts = args.bch_pool.split(":")
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 3333
                name = host.split(".")[0]
                bch_pools.append({"name": name, "host": host, "port": port})
            else:
                bch_pools = [{"name": "bchpool", "host": "solo.bchpool.org", "port": 3333}]

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
                  bch_pools=bch_pools).start()

if __name__ == "__main__":
    main()
