#ifndef SHA256_CUDA_H
#define SHA256_CUDA_H

#include <stdint.h>

#ifdef BUILDING_DLL
  #define DLL_EXPORT __declspec(dllexport)
#else
  #define DLL_EXPORT __declspec(dllimport)
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int device_id;
    int sm_count;
    int max_threads_per_block;
    int max_threads_per_sm;
    size_t shared_mem_per_block;
    size_t global_mem;
    int compute_major;
    int compute_minor;
    char name[256];
} GPUDeviceInfo;

typedef struct {
    uint32_t block_upper[8];
    uint32_t midstate[8];
    uint32_t target[8];
    uint8_t  header[80];
    uint32_t nonce_start;
    uint32_t nonce_end;
    uint32_t threads_per_block;
    uint32_t grid_size;
    int      double_buffer;
} MineConfig;

typedef struct {
    uint32_t nonces[256];
    uint32_t count;
    uint32_t hashes_processed;
} MineResult;

DLL_EXPORT int   gpu_init(int device_id);
DLL_EXPORT void  gpu_cleanup(void);
DLL_EXPORT int   gpu_get_device_count(void);
DLL_EXPORT int   gpu_get_device_info(int device_id, GPUDeviceInfo *info);
DLL_EXPORT int   gpu_setup(const uint8_t *header, const uint32_t *target,
                uint32_t nonce_start, uint32_t nonce_count,
                uint32_t threads_per_block, uint32_t grid_size);
DLL_EXPORT int   gpu_launch(void);
DLL_EXPORT int   gpu_launch_range(uint32_t nonce_start, uint64_t nonce_count);
DLL_EXPORT int   gpu_get_results(uint32_t *found_nonces, uint32_t max_count, uint32_t *count);
DLL_EXPORT int   gpu_autotune(const uint8_t *header, const uint32_t *target,
                   uint32_t *best_tpb, uint32_t *best_grid);

#ifdef __cplusplus
}
#endif

#endif
