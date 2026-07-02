// TrueCryptoMiner v10 - CUDA SHA-256d Mining Kernel (corrected)
//
// Data flow:
//   Bitcoin header: 80 bytes, fields in LE wire format
//   SHA-256 compress expects LE input words, internally swaps to BE
//   Host midstate: loads header LE, uses same compress (with swap)
//   Kernel: loads block2 LE, swaps to BE via compress
//   Double hash: first hash (LE words) -> second SHA-256 via compress
//   Hash result: 8 uint32 words in native (LE) byte order
//   Comparison: MSB at word index 7, LSB at word index 0
//   Target: 8 uint32 words packed as <8I (LE), MSB at index 7

#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define BUILDING_DLL
#include "sha256_cuda.h"

#define CUDA_CHECK(call) do { \
    cudaError_t err = (call); \
    if (err != cudaSuccess) { \
        fprintf(stderr, "CUDA error %s:%d: %s\n", __FILE__, __LINE__, \
                cudaGetErrorString(err)); \
        return 0; \
    } \
} while(0)

#define MAX_RESULTS 256

__constant__ uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a563f6, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

__constant__ uint32_t c_midstate[8];
__constant__ uint32_t c_target[8];
__constant__ uint32_t c_header2[4];
__shared__ uint32_t s_K[64];
__device__ uint32_t d_results[MAX_RESULTS];
__device__ uint32_t d_result_count;

__device__ __forceinline__ uint32_t rotr32(uint32_t x, int n) {
    return (x >> n) | (x << (32 - n));
}

__device__ __forceinline__ uint32_t swap32(uint32_t x) {
    return ((x & 0xFF000000u) >> 24) |
           ((x & 0x00FF0000u) >> 8)  |
           ((x & 0x0000FF00u) << 8)  |
           ((x & 0x000000FFu) << 24);
}

__device__ void store_nonce(uint32_t nonce) {
    uint32_t idx = atomicAdd(&d_result_count, 1);
    if (idx < MAX_RESULTS) d_results[idx] = nonce;
}

__global__ void kernel_mine_midstate(uint32_t nonce_base, uint64_t nonces_total) {
    uint32_t tid = blockIdx.x * blockDim.x + threadIdx.x;
    uint32_t stride = gridDim.x * blockDim.x;

    if (threadIdx.x < 64) s_K[threadIdx.x] = K[threadIdx.x];
    __syncthreads();

    // Load target: 8 uint32 words, MSB at index 7, LSB at index 0
    uint32_t target[8];
    for (int i = 0; i < 8; i++) target[i] = c_target[i];

    // Load midstate: 8 uint32 words in LE byte order (host computed with LE words)
    uint32_t midstate[8];
    for (int i = 0; i < 8; i++) midstate[i] = c_midstate[i];

    // Load header block 2 words (LE uint32 from host)
    uint32_t hdr2[4];
    for (int i = 0; i < 4; i++) hdr2[i] = c_header2[i];

    for (uint64_t i = tid; i < nonces_total; i += stride) {
        uint32_t nonce = nonce_base + (uint32_t)i;

        // Build second block: hdr2[0..2] + nonce + padding
        // All words in LE uint32 format (matching Bitcoin wire format)
        uint32_t block2[16];
        block2[0] = hdr2[0];       // LE
        block2[1] = hdr2[1];       // LE
        block2[2] = hdr2[2];       // LE
        block2[3] = nonce;          // LE uint32
        block2[4] = 0x80000000u;
        for (int j = 5; j < 15; j++) block2[j] = 0;
        block2[15] = 640;          // 80 bytes * 8 bits

        // ── First SHA-256: compress block2 ──
        // sha256_compress swaps LE input words to BE internally
        uint32_t state[8];
        for (int j = 0; j < 8; j++) state[j] = midstate[j];

        uint32_t W[64];
        for (int j = 0; j < 16; j++)
            W[j] = swap32(block2[j]);  // LE -> BE for SHA-256

        for (int j = 16; j < 64; j++) {
            uint32_t s0 = rotr32(W[j-15], 7) ^ rotr32(W[j-15], 18) ^ (W[j-15] >> 3);
            uint32_t s1 = rotr32(W[j-2], 17) ^ rotr32(W[j-2], 19) ^ (W[j-2] >> 10);
            W[j] = W[j-16] + s0 + W[j-7] + s1;
        }

        uint32_t a=state[0], b=state[1], c=state[2], d=state[3];
        uint32_t e=state[4], f=state[5], g=state[6], h=state[7];

        for (int j = 0; j < 64; j++) {
            uint32_t S1 = rotr32(e,6) ^ rotr32(e,11) ^ rotr32(e,25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = h + S1 + ch + s_K[j] + W[j];
            uint32_t S0 = rotr32(a,2) ^ rotr32(a,13) ^ rotr32(a,22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        state[0]+=a; state[1]+=b; state[2]+=c; state[3]+=d;
        state[4]+=e; state[5]+=f; state[6]+=g; state[7]+=h;
        // state[0..7] now holds first SHA-256 hash in LE byte order

        // ── Second SHA-256: hash the 32-byte first hash ──
        uint32_t hash_state[8] = {
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
        };

        // Pad first hash to 64 bytes: data(32) + 0x80 + zeros + length(256)
        uint32_t h2[16];
        for (int j = 0; j < 8; j++)
            h2[j] = state[j];  // LE words (first hash)
        h2[8] = 0x80000000u;
        for (int j = 9; j < 15; j++) h2[j] = 0;
        h2[15] = 256;  // 32 bytes * 8 bits

        // Compress (swap32 converts LE -> BE for SHA-256)
        uint32_t W2[64];
        for (int j = 0; j < 16; j++)
            W2[j] = swap32(h2[j]);  // LE -> BE

        for (int j = 16; j < 64; j++) {
            uint32_t s0 = rotr32(W2[j-15], 7) ^ rotr32(W2[j-15], 18) ^ (W2[j-15] >> 3);
            uint32_t s1 = rotr32(W2[j-2], 17) ^ rotr32(W2[j-2], 19) ^ (W2[j-2] >> 10);
            W2[j] = W2[j-16] + s0 + W2[j-7] + s1;
        }

        a=hash_state[0]; b=hash_state[1]; c=hash_state[2]; d=hash_state[3];
        e=hash_state[4]; f=hash_state[5]; g=hash_state[6]; h=hash_state[7];
        for (int j = 0; j < 64; j++) {
            uint32_t S1 = rotr32(e,6) ^ rotr32(e,11) ^ rotr32(e,25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = h + S1 + ch + s_K[j] + W2[j];
            uint32_t S0 = rotr32(a,2) ^ rotr32(a,13) ^ rotr32(a,22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        hash_state[0]+=a; hash_state[1]+=b; hash_state[2]+=c; hash_state[3]+=d;
        hash_state[4]+=e; hash_state[5]+=f; hash_state[6]+=g; hash_state[7]+=h;
        // hash_state[0..7] = double-SHA256 result in LE byte order

        // ── Compare against target ──
        // hash_state[0] = MSB word, hash_state[7] = LSB word
        // target[0] = MSB, target[7] = LSB (packed as <8I)
        // Compare from index 0 (MSB) to index 7 (LSB)
        for (int j = 0; j < 8; j++) {
            if (hash_state[j] < target[j]) { store_nonce(nonce); break; }
            if (hash_state[j] > target[j]) break;
        }
    }
}

// ─── HOST ────────────────────────────────────────────────────

static int g_device_initialized = 0;
static int g_device_id = -1;
static cudaDeviceProp g_props;
static uint32_t *d_midstate = NULL;
static uint32_t *d_target_dev = NULL;
static uint32_t *d_header2 = NULL;
static uint32_t *d_results_dev = NULL;
static uint32_t *d_result_count_dev = NULL;
static uint32_t g_best_tpb = 256;
static uint32_t g_best_grid = 80;
static cudaStream_t g_stream_a, g_stream_b;
static cudaEvent_t g_event_a, g_event_b;
static int g_stream_initialized = 0;

// Host SHA-256 compress: matches kernel exactly (swap32 on input words)
static void host_compress(uint32_t *state, const uint32_t *block) {
    uint32_t W[64];
    static const uint32_t KK[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a563f6,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
    };
    for (int i = 0; i < 16; i++) W[i] = ((block[i]>>24)&0xFF)|((block[i]>>8)&0xFF00)|((block[i]<<8)&0xFF0000)|((block[i]<<24)&0xFF000000);
    for (int i = 16; i < 64; i++) {
        uint32_t s0=((W[i-15]>>7)|(W[i-15]<<25))^((W[i-15]>>18)|(W[i-15]<<14))^(W[i-15]>>3);
        uint32_t s1=((W[i-2]>>17)|(W[i-2]<<15))^((W[i-2]>>19)|(W[i-2]<<13))^(W[i-2]>>10);
        W[i]=W[i-16]+s0+W[i-7]+s1;
    }
    uint32_t a=state[0],b=state[1],c=state[2],d=state[3],e=state[4],f=state[5],g=state[6],h=state[7],t1,t2;
    for (int i=0;i<64;i++){
        uint32_t S1=((e>>6)|(e<<26))^((e>>11)|(e<<21))^((e>>25)|(e<<7));
        uint32_t ch=(e&f)^(~e&g); t1=h+S1+ch+KK[i]+W[i];
        uint32_t S0=((a>>2)|(a<<30))^((a>>13)|(a<<19))^((a>>22)|(a<<10));
        uint32_t mj=(a&b)^(a&c)^(b&c); t2=S0+mj;
        h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    state[0]+=a;state[1]+=b;state[2]+=c;state[3]+=d;state[4]+=e;state[5]+=f;state[6]+=g;state[7]+=h;
}

// Compute midstate: SHA-256 of first 64 bytes of header
// Header is in Bitcoin wire format (LE fields)
// Loads as LE uint32, compress swaps to BE internally
static void compute_midstate(const uint8_t *header, uint32_t *midstate_out, uint32_t *header2_out) {
    uint32_t W[16];
    for (int i = 0; i < 16; i++) {
        const uint8_t *p = header + i * 4;
        W[i] = (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
    }
    uint32_t state[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    host_compress(state, W);
    for (int i=0;i<8;i++) midstate_out[i]=state[i];
    for (int i=0;i<4;i++) {
        const uint8_t *p=header+64+i*4;
        header2_out[i]=(uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
    }
}

extern "C" {

int gpu_get_device_count(void) { int c=0; cudaGetDeviceCount(&c); return c; }

int gpu_get_device_info(int id, GPUDeviceInfo *info) {
    if(!info)return 0; cudaDeviceProp p; if(cudaGetDeviceProperties(&p,id)!=cudaSuccess)return 0;
    info->device_id=id;info->sm_count=p.multiProcessorCount;info->max_threads_per_block=p.maxThreadsPerBlock;
    info->max_threads_per_sm=p.maxThreadsPerMultiProcessor;info->shared_mem_per_block=p.sharedMemPerBlock;
    info->global_mem=p.totalGlobalMem;info->compute_major=p.major;info->compute_minor=p.minor;
    strncpy(info->name,p.name,255);info->name[255]=0;return 1;
}

int gpu_init(int device_id) {
    int c=gpu_get_device_count();if(c==0)return 0;if(device_id<0||device_id>=c)device_id=0;
    CUDA_CHECK(cudaSetDevice(device_id));CUDA_CHECK(cudaGetDeviceProperties(&g_props,device_id));
    printf("[GPU] Device %d: %s\n",device_id,g_props.name);
    printf("[GPU]   SMs: %d, Max threads/block: %d, Compute: %d.%d\n",g_props.multiProcessorCount,g_props.maxThreadsPerBlock,g_props.major,g_props.minor);
    CUDA_CHECK(cudaMalloc(&d_results_dev,MAX_RESULTS*sizeof(uint32_t)));
    CUDA_CHECK(cudaMalloc(&d_result_count_dev,sizeof(uint32_t)));CUDA_CHECK(cudaMemset(d_result_count_dev,0,sizeof(uint32_t)));
    CUDA_CHECK(cudaMalloc(&d_midstate,8*sizeof(uint32_t)));CUDA_CHECK(cudaMalloc(&d_target_dev,8*sizeof(uint32_t)));
    CUDA_CHECK(cudaMalloc(&d_header2,4*sizeof(uint32_t)));
    g_device_id=device_id;g_device_initialized=1;
    if(!g_stream_initialized){cudaStreamCreate(&g_stream_a);cudaStreamCreate(&g_stream_b);cudaEventCreate(&g_event_a);cudaEventCreate(&g_event_b);g_stream_initialized=1;}
    return 1;
}

void gpu_cleanup(void) {
    if(d_results_dev){cudaFree(d_results_dev);d_results_dev=NULL;}
    if(d_result_count_dev){cudaFree(d_result_count_dev);d_result_count_dev=NULL;}
    if(d_midstate){cudaFree(d_midstate);d_midstate=NULL;}
    if(d_target_dev){cudaFree(d_target_dev);d_target_dev=NULL;}
    if(d_header2){cudaFree(d_header2);d_header2=NULL;}
    if(g_stream_initialized){cudaStreamDestroy(g_stream_a);cudaStreamDestroy(g_stream_b);cudaEventDestroy(g_event_a);cudaEventDestroy(g_event_b);g_stream_initialized=0;}
    if(g_device_id>=0){cudaDeviceReset();g_device_id=-1;}g_device_initialized=0;printf("[GPU] Cleanup complete\n");
}

int gpu_setup(const uint8_t *header, const uint32_t *target, uint32_t ns, uint32_t nc, uint32_t tpb, uint32_t grid) {
    if(!g_device_initialized)return 0;
    uint32_t ms[8],h2[4];compute_midstate(header,ms,h2);
    cudaMemcpyToSymbol(c_midstate,ms,8*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_target,target,8*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_header2,h2,4*sizeof(uint32_t));
    g_best_tpb=tpb;g_best_grid=grid;return 1;
}

int gpu_launch(void) {
    if(!g_device_initialized)return 0;uint32_t z=0;cudaMemcpyToSymbol(d_result_count,&z,sizeof(uint32_t));
    kernel_mine_midstate<<<g_best_grid,g_best_tpb>>>(0,g_best_grid*g_best_tpb);
    CUDA_CHECK(cudaGetLastError());CUDA_CHECK(cudaDeviceSynchronize());return 1;
}

int gpu_launch_range(uint32_t ns, uint64_t nc) {
    if(!g_device_initialized)return 0;uint32_t z=0;cudaMemcpyToSymbol(d_result_count,&z,sizeof(uint32_t));
    kernel_mine_midstate<<<g_best_grid,g_best_tpb>>>(ns,nc);
    cudaError_t e=cudaGetLastError();if(e!=cudaSuccess){fprintf(stderr,"[GPU] Launch error: %s\n",cudaGetErrorString(e));return 0;}
    e=cudaDeviceSynchronize();if(e!=cudaSuccess){fprintf(stderr,"[GPU] Exec error: %s\n",cudaGetErrorString(e));return 0;}
    return 1;
}

int gpu_get_results(uint32_t *fn, uint32_t mc, uint32_t *cnt) {
    if(!g_device_initialized||!fn||!cnt)return 0;uint32_t gc=0;
    cudaMemcpyFromSymbol(&gc,d_result_count,sizeof(uint32_t));
    if(gc==0){*cnt=0;return 1;}uint32_t tc=(gc<mc)?gc:mc;
    uint32_t tmp[MAX_RESULTS],rc=(gc<MAX_RESULTS)?gc:MAX_RESULTS;
    uint32_t *dp;cudaGetSymbolAddress((void**)&dp,d_results);
    cudaMemcpy(tmp,dp,rc*sizeof(uint32_t),cudaMemcpyDeviceToHost);
    memcpy(fn,tmp,tc*sizeof(uint32_t));*cnt=tc;return 1;
}

int gpu_autotune(const uint8_t *header, const uint32_t *target, uint32_t *btp, uint32_t *bg) {
    if(!g_device_initialized)return 0;
    struct{uint32_t t,g;}cfg[]={{128,320},{128,480},{256,160},{256,240},{256,320},{512,80},{512,120},{512,160},{1024,40},{1024,60},{1024,80}};
    uint32_t tn=1024*1024,ms[8],h2[4];compute_midstate(header,ms,h2);
    cudaMemcpyToSymbol(c_midstate,ms,8*sizeof(uint32_t));cudaMemcpyToSymbol(c_target,target,8*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_header2,h2,4*sizeof(uint32_t));
    double bt=1e18;uint32_t bt2=256,bg2=80;
    for(int i=0;i<11;i++){
        uint32_t t=cfg[i].t,g=cfg[i].g;
        if(t>(uint32_t)g_props.maxThreadsPerBlock)continue;
        if(t*g>(uint32_t)g_props.maxThreadsPerMultiProcessor*g_props.multiProcessorCount*2)continue;
        uint32_t z=0;cudaMemcpyToSymbol(d_result_count,&z,sizeof(uint32_t));
        kernel_mine_midstate<<<g,t>>>(0,tn);cudaDeviceSynchronize();
        cudaEvent_t s,sp;cudaEventCreate(&s);cudaEventCreate(&sp);
        cudaEventRecord(s);z=0;cudaMemcpyToSymbol(d_result_count,&z,sizeof(uint32_t));
        kernel_mine_midstate<<<g,t>>>(0,tn);cudaEventRecord(sp);cudaEventSynchronize(sp);
        float ms2=0;cudaEventElapsedTime(&ms2,s,sp);cudaEventDestroy(s);cudaEventDestroy(sp);
        double el=ms2/1000.0;printf("[GPU]   tpb=%u grid=%u: %.2f ms = %.2f MH/s\n",t,g,ms2,tn/el/1e6);
        if(el<bt){bt=el;bt2=t;bg2=g;}
    }
    printf("[GPU] Best: tpb=%u grid=%u (%.2f MH/s)\n",bt2,bg2,tn/bt/1e6);
    g_best_tpb=bt2;g_best_grid=bg2;*btp=bt2;*bg=bg2;return 1;
}

}
