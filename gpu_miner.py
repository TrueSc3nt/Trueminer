#!/usr/bin/env python3
"""
GPU Mining Engine - Python wrapper for sha256_cuda.dll
Provides auto-tuning, double-buffering, and ctypes bindings.
"""

import os, sys, time, ctypes, struct, hashlib, binascii, threading
from typing import List, Optional, Tuple

# ─── CUDA DLL Loading ─────────────────────────────────────────

class GPUInfo:
    def __init__(self):
        self.device_id = 0
        self.sm_count = 0
        self.max_threads_per_block = 0
        self.max_threads_per_sm = 0
        self.shared_mem_per_block = 0
        self.global_mem = 0
        self.compute_major = 0
        self.compute_minor = 0
        self.name = ""

class GPUMiner:
    """CUDA GPU miner wrapper with auto-tuning and double-buffering."""

    def __init__(self, script_dir: str = None):
        self.lib = None
        self.available = False
        self.device_info: Optional[GPUInfo] = None
        self.best_tpb = 256
        self.best_grid = 80
        self.lock = threading.Lock()
        self._last_header = None
        self._last_target = None

        if script_dir is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))

        self._load_library(script_dir)

    def _load_library(self, script_dir: str):
        candidates = [
            os.path.join(script_dir, 'sha256_cuda.dll'),
            os.path.join(script_dir, 'sha256_cuda.so'),
            'sha256_cuda.dll',
            'sha256_cuda.so',
        ]

        for path in candidates:
            if os.path.exists(path):
                try:
                    lib = ctypes.CDLL(path)

                    # Set up function signatures
                    lib.gpu_get_device_count.argtypes = []
                    lib.gpu_get_device_count.restype = ctypes.c_int

                    lib.gpu_get_device_info.argtypes = [
                        ctypes.c_int, ctypes.POINTER(ctypes.c_char * 512)]
                    lib.gpu_get_device_info.restype = ctypes.c_int

                    lib.gpu_init.argtypes = [ctypes.c_int]
                    lib.gpu_init.restype = ctypes.c_int

                    lib.gpu_cleanup.argtypes = []
                    lib.gpu_cleanup.restype = None

                    lib.gpu_setup.argtypes = [
                        ctypes.c_char_p,           # header
                        ctypes.c_char_p,           # target (big-endian 32 bytes)
                        ctypes.c_uint32,           # nonce_start
                        ctypes.c_uint32,           # nonce_count
                        ctypes.c_uint32,           # threads_per_block
                        ctypes.c_uint32,           # grid_size
                    ]
                    lib.gpu_setup.restype = ctypes.c_int

                    lib.gpu_launch.argtypes = []
                    lib.gpu_launch.restype = ctypes.c_int

                    lib.gpu_launch_range.argtypes = [
                        ctypes.c_uint32, ctypes.c_uint64]
                    lib.gpu_launch_range.restype = ctypes.c_int

                    lib.gpu_get_results.argtypes = [
                        ctypes.POINTER(ctypes.c_uint32),  # found_nonces
                        ctypes.c_uint32,                   # max_count
                        ctypes.POINTER(ctypes.c_uint32),   # count
                    ]
                    lib.gpu_get_results.restype = ctypes.c_int

                    lib.gpu_autotune.argtypes = [
                        ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.POINTER(ctypes.c_uint32),
                        ctypes.POINTER(ctypes.c_uint32),
                    ]
                    lib.gpu_autotune.restype = ctypes.c_int

                    self.lib = lib
                    print(f"[GPU] Loaded CUDA library from {path}")
                    return
                except Exception as e:
                    print(f"[GPU] Failed to load {path}: {e}")

        print("[GPU] CUDA library not found. GPU mining disabled.")
        print("[GPU] Run build_gpu.bat to compile the CUDA kernel.")

    def init(self, device_id: int = -1) -> bool:
        if not self.lib:
            return False

        device_count = self.lib.gpu_get_device_count()
        if device_count == 0:
            print("[GPU] No CUDA devices found")
            return False

        if device_id < 0:
            # Auto-select: pick the best GPU
            device_id = 0
            best_sm = 0
            for i in range(device_count):
                info = GPUInfo()
                # Simplified - just pick first device
                device_id = i
                break

        if not self.lib.gpu_init(device_id):
            return False

        self.device_info = GPUInfo()
        self.device_info.device_id = device_id

        self.available = True
        return True

    def cleanup(self):
        if self.lib and self.available:
            self.lib.gpu_cleanup()
            self.available = False

    def _target_to_big_endian(self, target_int: int) -> bytes:
        """Convert target integer to bytes for the C kernel.
        Kernel compares from index 0 (MSB) to index 7 (LSB).
        Hash words are in LE byte order (native).
        Target: 8 uint32 words packed as <8I, MSB at index 0."""
        import struct
        words = []
        for i in range(8):
            word = (target_int >> (224 - i*32)) & 0xFFFFFFFF
            words.append(word)
        return struct.pack('<8I', *words)

    def _header_to_bytes(self, header_hex: str) -> bytes:
        """Convert hex header string to bytes."""
        return binascii.unhexlify(header_hex)

    def setup_and_autotune(self, header_hex: str, target_int: int,
                           nonce_start: int = 0, nonce_count: int = 2**32) -> bool:
        if not self.available:
            return False

        header_bytes = self._header_to_bytes(header_hex)
        target_bytes = self._target_to_big_endian(target_int)

        # Auto-tune
        best_tpb = ctypes.c_uint32(0)
        best_grid = ctypes.c_uint32(0)

        with self.lock:
            if not self.lib.gpu_autotune(
                header_bytes, target_bytes,
                ctypes.byref(best_tpb), ctypes.byref(best_grid)):
                print("[GPU] Auto-tune failed, using defaults")
                best_tpb.value = self.best_tpb
                best_grid.value = self.best_grid

            self.best_tpb = best_tpb.value
            self.best_grid = best_grid.value

        # Setup with tuned parameters
        return bool(self.lib.gpu_setup(
            header_bytes, target_bytes,
            nonce_start, nonce_count,
            self.best_tpb, self.best_grid))

    def mine_batch(self, header_hex: str, target_int: int,
                   nonce_start: int, nonce_count: int) -> List[int]:
        """
        Mine a batch of nonces on the GPU.
        Returns list of nonces that meet the target.
        """
        if not self.available:
            return []

        header_bytes = self._header_to_bytes(header_hex)
        target_bytes = self._target_to_big_endian(target_int)

        # Suppress C-level printf from DLL
        import os as _os
        def _quiet(func, *a, **kw):
            _dn = _os.open(_os.devnull, _os.O_WRONLY)
            _old = _os.dup(1)
            _os.dup2(_dn, 1)
            try: r = func(*a, **kw)
            finally: _os.dup2(_old, 1); _os.close(_old); _os.close(_dn)
            return r

        with self.lock:
            # Only re-setup GPU if header or target changed
            if header_bytes != self._last_header or target_bytes != self._last_target:
                _quiet(self.lib.gpu_setup,
                    header_bytes, target_bytes,
                    nonce_start, nonce_count,
                    self.best_tpb, self.best_grid)
                self._last_header = header_bytes
                self._last_target = target_bytes

            # Launch
            _quiet(self.lib.gpu_launch_range, nonce_start, nonce_count)

            # Get results
            found = (ctypes.c_uint32 * 256)()
            count = ctypes.c_uint32(0)
            self.lib.gpu_get_results(found, 256, ctypes.byref(count))

        return [found[i] for i in range(count.value)]

    def mine_continuous(self, header_hex: str, target_int: int,
                        result_callback=None,
                        nonce_chunk: int = 4 * 1024 * 1024):
        """
        Mine continuously through the full 32-bit nonce space.
        Uses double-buffering: while GPU mines chunk N, prepare chunk N+1.
        Calls result_callback(nonce, hash_hex) for each found nonce.
        """
        if not self.available:
            return

        header_bytes = self._header_to_bytes(header_hex)
        target_bytes = self._target_to_big_endian(target_int)

        total = 2**32
        chunk = nonce_chunk
        position = 0

        with self.lock:
            self.lib.gpu_setup(
                header_bytes, target_bytes,
                0, total,
                self.best_tpb, self.best_grid)

        # Launch first chunk
        self.lib.gpu_launch_range(0, min(chunk, total))
        position = chunk

        while position < total:
            # Get results from previous launch
            found = (ctypes.c_uint32 * 256)()
            count = ctypes.c_uint32(0)
            self.lib.gpu_get_results(found, 256, ctypes.byref(count))

            for i in range(count.value):
                nonce = found[i]
                if result_callback:
                    # Compute the hash for display
                    hdr = header_hex + format(nonce, '08x')
                    raw = binascii.unhexlify(hdr)
                    h = hashlib.sha256(hashlib.sha256(raw, usedforsecurity=False).digest(), usedforsecurity=False).digest()
                    hash_hex = binascii.hexlify(h).decode()
                    result_callback(nonce, hash_hex)

            # Launch next chunk
            end = min(position + chunk, total)
            self.lib.gpu_launch_range(position, end - position)
            position = end

        # Final results
        found = (ctypes.c_uint32 * 256)()
        count = ctypes.c_uint32(0)
        self.lib.gpu_get_results(found, 256, ctypes.byref(count))

        for i in range(count.value):
            nonce = found[i]
            if result_callback:
                hdr = header_hex + format(nonce, '08x')
                raw = binascii.unhexlify(hdr)
                h = hashlib.sha256(hashlib.sha256(raw, usedforsecurity=False).digest(), usedforsecurity=False).digest()
                hash_hex = binascii.hexlify(h).decode()
                result_callback(nonce, hash_hex)

    def get_info(self) -> dict:
        if not self.available:
            return {"available": False}
        return {
            "available": True,
            "device_id": self.device_info.device_id,
            "best_tpb": self.best_tpb,
            "best_grid": self.best_grid,
        }


# ─── Standalone test ──────────────────────────────────────────

if __name__ == "__main__":
    gpu = GPUMiner()
    if gpu.init():
        print(f"GPU available: {gpu.get_info()}")
        print("To mine, run c.py with --gpu flag")
    else:
        print("No GPU available")
