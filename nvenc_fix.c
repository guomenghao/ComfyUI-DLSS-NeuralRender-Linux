/* SPDX-License-Identifier: GPL-3.0-only
 * Based on flexgrip/nvidia-gpu-enumeration, commit
 * 1475091ae9e8fb23cc22d565adf932a488d00bcd (2026).
 * Local changes: exact ioctl/ABI/device checks; documented PCI ID lookup;
 * unambiguous PCI fallback; partial lookup handling; initialize before threads.
 * GPL terms: NVENC_FIX_LICENSE.txt. This is an experimental userspace workaround.
 */
/*
 * nvenc_fix.c - LD_PRELOAD interposer to fix NVENC in multi-GPU containers
 *
 * Problem: On NVIDIA driver >= 570, when NVENC initializes inside a container
 * that has only 1 GPU assigned, libnvidia-encode queries /dev/nvidiactl for
 * ALL host GPUs (NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS, RM ioctl 0x2A, command 0x0201). When it
 * sees multiple GPUs, it tries to peer-init with the others, fails because
 * their /dev/nvidiaX nodes aren't mounted, and returns NV_ENC_ERR_UNSUPPORTED_DEVICE.
 *
 * Fix: Intercept the ioctl() call, let it pass through to the real kernel driver,
 * then post-process the GET_ATTACHED_IDS response to only include GPUs whose
 * /dev/nvidiaX device nodes actually exist in this container.
 *
 * Build:
 *   gcc -shared -fPIC -o libnvenc_fix.so nvenc_fix.c -ldl
 *
 * Usage:
 *   LD_PRELOAD=/path/to/libnvenc_fix.so ffmpeg -hwaccel cuda ...
 *
 * Or in Dockerfile:
 *   ENV LD_PRELOAD=/opt/libnvenc_fix.so
 *
 * Logging (NVENC_FIX_DEBUG environment variable):
 *   unset or empty                      → no logging (production default)
 *   NVENC_FIX_DEBUG=1                   → log to stderr
 *   NVENC_FIX_DEBUG=stderr              → log to stderr
 *   NVENC_FIX_DEBUG=/tmp/nvenc_fix.log  → log to file (appended)
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <errno.h>
#include <sys/sysmacros.h>

/* ============================================================
 * NVIDIA RM ioctl structures (from open-gpu-kernel-modules)
 * ============================================================ */

typedef uint32_t NvV32;
typedef uint32_t NvU32;
typedef NvU32    NvHandle;
typedef void*    NvP64;

#define NV_ALIGN_BYTES(size) __attribute__((aligned(size)))

#define NV_IOCTL_MAGIC 'F'
#define NV_ESC_RM_CONTROL 0x2A

/* NVOS54_PARAMETERS - the RM control ioctl parameter struct */
typedef struct {
    NvHandle hClient;
    NvHandle hObject;
    NvV32    cmd;
    NvU32    flags;
    NvP64    params NV_ALIGN_BYTES(8);
    NvU32    paramsSize;
    NvV32    status;
} NVOS54_PARAMETERS;

/* NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS = 0x0201
 * The gpuIds array is NV0000_CTRL_GPU_MAX_ATTACHED_GPUS = 32 entries */
#define NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS 0x0201
#define NV0000_CTRL_GPU_MAX_ATTACHED_GPUS    32
#define NV0000_CTRL_GPU_INVALID_ID           0xFFFFFFFF

typedef struct {
    NvU32 gpuIds[NV0000_CTRL_GPU_MAX_ATTACHED_GPUS];
} NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS;

/* 610.57.04: GET_PCI_INFO returns a PCI address for an opaque GPU ID.
 * deviceInstance is an RM broadcast instance, not the Linux device minor.
 */
#define NV0000_CTRL_CMD_GPU_GET_PCI_INFO 0x021b

typedef struct {
    NvU32 gpuId;
    NvU32 domain;
    uint16_t bus;
    uint16_t slot;
} NV0000_CTRL_GPU_GET_PCI_INFO_PARAMS;
_Static_assert(sizeof(NVOS54_PARAMETERS) == 32, "Linux x64 RM control ABI");
_Static_assert(sizeof(NV0000_CTRL_GPU_GET_PCI_INFO_PARAMS) == 12, "GPU PCI ABI");

/* ============================================================
 * Logging
 * ============================================================ */

static int   log_initialized = 0;
static int   log_enabled     = 0;
static FILE *log_file        = NULL;

/*
 * NVENC_FIX_DEBUG controls logging:
 *   unset / empty       → logging disabled
 *   "1" or "stderr"     → log to stderr
 *   "/some/path.log"    → log to that file (appended)
 */
static void log_init(void) {
    if (log_initialized)
        return;
    log_initialized = 1;

    const char *val = getenv("NVENC_FIX_DEBUG");
    if (!val || val[0] == '\0') {
        log_enabled = 0;
        return;
    }

    log_enabled = 1;

    if (strcmp(val, "1") == 0 || strcmp(val, "stderr") == 0) {
        log_file = stderr;
    } else {
        log_file = fopen(val, "a");
        if (!log_file) {
            /* Fall back to stderr if the file can't be opened */
            log_file = stderr;
            fprintf(stderr, "[nvenc_fix] WARNING: could not open log file '%s', falling back to stderr\n", val);
        }
    }
}

static void log_msg(const char *fmt, ...) {
    log_init();
    if (!log_enabled)
        return;
    va_list ap;
    va_start(ap, fmt);
    fprintf(log_file, "[nvenc_fix] ");
    vfprintf(log_file, fmt, ap);
    fprintf(log_file, "\n");
    fflush(log_file);
    va_end(ap);
}

/* ============================================================
 * Real ioctl
 * ============================================================ */

typedef int (*ioctl_fn_t)(int fd, unsigned long request, ...);
static ioctl_fn_t real_ioctl = NULL;

static void ensure_real_ioctl(void) {
    if (!real_ioctl) {
        real_ioctl = (ioctl_fn_t)dlsym(RTLD_NEXT, "ioctl");
        if (!real_ioctl) {
            fprintf(stderr, "[nvenc_fix] FATAL: cannot find real ioctl: %s\n", dlerror());
            _exit(1);
        }
    }
}

/* ============================================================
 * Check if /dev/nvidiaX exists in this container
 * ============================================================ */

static int device_node_exists(NvU32 device_instance) {
    char path[64];
    snprintf(path, sizeof(path), "/dev/nvidia%u", device_instance);
    struct stat st;
    if (stat(path, &st) != 0 || !S_ISCHR(st.st_mode) ||
        major(st.st_rdev) != 195 || (NvU32)minor(st.st_rdev) != device_instance)
        return 0;
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) return 0;
    close(fd);
    return 1;
}

/* ============================================================
 * Determine which device instances are available.
 * We scan /dev/nvidia0 through /dev/nvidia31.
 * Returns a bitmask of available device instances.
 * ============================================================ */

static uint32_t get_available_devices(void) {
    uint32_t mask = 0;
    for (int i = 0; i < 32; i++) {
        if (device_node_exists(i))
            mask |= (1u << i);
    }
    return mask;
}

/* ============================================================
 * Parse /proc/driver/nvidia/gpus/ to build a mapping from
 * PCI bus location → Device Minor number.
 *
 * Each GPU has a directory like /proc/driver/nvidia/gpus/0001:18:00.0/
 * containing an "information" file with "Device Minor: N" and
 * "Bus Location: XXXX:XX:XX.X" fields.
 *
 * We use this to figure out which gpuId corresponds to the
 * /dev/nvidiaX nodes that exist in this container.
 * ============================================================ */

#include <dirent.h>

#define MAX_GPU_MAP 32

typedef struct {
    unsigned int domain;
    unsigned int bus;
    unsigned int slot;
    unsigned int func;
    int          device_minor;
} gpu_proc_entry_t;

static int gpu_map_count = 0;
static gpu_proc_entry_t gpu_map[MAX_GPU_MAP];
static int gpu_map_loaded = 0;

static void load_gpu_map(void) {
    if (gpu_map_loaded)
        return;
    gpu_map_loaded = 1;
    gpu_map_count = 0;

    DIR *dir = opendir("/proc/driver/nvidia/gpus");
    if (!dir) {
        log_msg("WARNING: cannot open /proc/driver/nvidia/gpus");
        return;
    }

    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL && gpu_map_count < MAX_GPU_MAP) {
        if (ent->d_name[0] == '.')
            continue;

        /* Parse PCI address from directory name: "0001:18:00.0" */
        unsigned int domain, bus, slot, func;
        if (sscanf(ent->d_name, "%x:%x:%x.%x", &domain, &bus, &slot, &func) != 4)
            continue;

        /* Read the information file to get Device Minor */
        char info_path[512];
        snprintf(info_path, sizeof(info_path),
                 "/proc/driver/nvidia/gpus/%s/information", ent->d_name);

        FILE *f = fopen(info_path, "r");
        if (!f)
            continue;

        int dev_minor = -1;
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            if (sscanf(line, "Device Minor: %d", &dev_minor) == 1)
                break;
        }
        fclose(f);

        if (dev_minor < 0)
            continue;

        gpu_map[gpu_map_count].domain       = domain;
        gpu_map[gpu_map_count].bus          = bus;
        gpu_map[gpu_map_count].slot         = slot;
        gpu_map[gpu_map_count].func         = func;
        gpu_map[gpu_map_count].device_minor = dev_minor;
        gpu_map_count++;

        log_msg("proc map: %04x:%02x:%02x.%x -> Device Minor %d",
                domain, bus, slot, func, dev_minor);
    }
    closedir(dir);

    log_msg("loaded %d GPU entries from /proc", gpu_map_count);
}

/* Exact PCI domain, bus and slot matching; reject duplicate functions too. */
static int match_pci_to_minor(NvU32 domain, unsigned int bus, unsigned int slot) {
    load_gpu_map();
    int matched = -1, count = 0;
    for (int i = 0; i < gpu_map_count; i++) {
        if (gpu_map[i].domain == domain && gpu_map[i].bus == bus &&
            gpu_map[i].slot == slot) {
            matched = gpu_map[i].device_minor;
            count++;
        }
    }
    return count == 1 ? matched : -1;
}

static int resolve_gpu_id_to_minor(int fd, NvHandle hClient, NvU32 gpuId) {
    NV0000_CTRL_GPU_GET_PCI_INFO_PARAMS pci = { .gpuId = gpuId };
    NVOS54_PARAMETERS query = { .hClient = hClient, .hObject = hClient,
        .cmd = NV0000_CTRL_CMD_GPU_GET_PCI_INFO, .params = &pci,
        .paramsSize = sizeof(pci) };
    unsigned long request = _IOC(_IOC_READ | _IOC_WRITE, NV_IOCTL_MAGIC,
                                NV_ESC_RM_CONTROL, sizeof(query));
    int result = real_ioctl(fd, request, &query);
    if (result != 0 || query.status != 0) {
        log_msg("GET_PCI_INFO failed for gpuId 0x%x: ioctl=%d status=0x%x; not filtering",
                gpuId, result, query.status);
        return -1;
    }
    int device_minor = match_pci_to_minor(pci.domain, pci.bus, pci.slot);
    log_msg("gpuId 0x%x -> PCI %04x:%02x:%02x -> Device Minor %d",
            gpuId, pci.domain, pci.bus, pci.slot, device_minor);
    return device_minor;
}

/* ============================================================
 * Main ioctl interposer
 * ============================================================ */

int ioctl(int fd, unsigned long request, ...) {
    ensure_real_ioctl();

    /* Extract the vararg pointer */
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);

    /* Pass through to real ioctl first */
    int ret = real_ioctl(fd, request, arg);

    /* Only process successful ioctls */
    if (ret != 0)
        return ret;

    /* Check if this is an NV_ESC_RM_CONTROL ioctl.
     * The ioctl number encodes the escape code in bits 0-7 of the NR field. */
    unsigned int nr = _IOC_NR(request);
    if (nr != NV_ESC_RM_CONTROL || _IOC_TYPE(request) != NV_IOCTL_MAGIC ||
        _IOC_SIZE(request) != sizeof(NVOS54_PARAMETERS) ||
        _IOC_DIR(request) != (_IOC_READ | _IOC_WRITE))
        return ret;
    struct stat device;
    if (fstat(fd, &device) != 0 || !S_ISCHR(device.st_mode) ||
        major(device.st_rdev) != 195 || minor(device.st_rdev) != 255)
        return ret;

    /* It's an RM_CONTROL. Check the cmd field. */
    NVOS54_PARAMETERS *ctrl = (NVOS54_PARAMETERS *)arg;
    if (!ctrl || !ctrl->params || ctrl->flags != 0 ||
        ctrl->paramsSize != sizeof(NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS))
        return ret;

    if (ctrl->cmd != NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS)
        return ret;

    if (ctrl->status != 0)
        return ret;

    /* === This is the GET_ATTACHED_IDS response. Filter it. === */

    NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS *gpu_params =
        (NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS *)ctrl->params;

    /* Count how many GPUs the kernel reported */
    int total_host_gpus = 0;
    for (int i = 0; i < NV0000_CTRL_GPU_MAX_ATTACHED_GPUS; i++) {
        if (gpu_params->gpuIds[i] == NV0000_CTRL_GPU_INVALID_ID)
            break;
        total_host_gpus++;
    }

    log_msg("GET_ATTACHED_IDS returned %d GPUs from host", total_host_gpus);

    if (total_host_gpus <= 1) {
        /* Single GPU or empty, nothing to filter */
        return ret;
    }

    /* Get the bitmask of /dev/nvidiaX nodes that exist in this container */
    uint32_t available = get_available_devices();
    log_msg("available device node bitmask: 0x%08x", available);

    if (available == 0) {
        /* Can't determine available devices, don't filter (fail-safe) */
        log_msg("WARNING: no /dev/nvidiaX nodes found, not filtering");
        return ret;
    }

    /* GPU IDs remain opaque: ask RM for PCI coordinates, then read the
     * corresponding Linux minor from /proc. Never equate deviceInstance
     * with a minor, and never derive bus numbers by shifting GPU IDs.
     */
    NvU32 filtered[NV0000_CTRL_GPU_MAX_ATTACHED_GPUS];
    int filtered_count = 0;
    for (int i = 0; i < total_host_gpus; i++) {
        int device_minor = resolve_gpu_id_to_minor(fd, ctrl->hClient, gpu_params->gpuIds[i]);
        if (device_minor < 0) {
            log_msg("WARNING: incomplete or ambiguous PCI map; original GPU list retained");
            return ret;
        }
        if (device_minor < 32 && (available & (1u << device_minor))) {
            log_msg("KEEPING gpuId 0x%x (Device Minor %d via PCI)",
                    gpu_params->gpuIds[i], device_minor);
            filtered[filtered_count++] = gpu_params->gpuIds[i];
        } else {
            log_msg("REMOVING gpuId 0x%x (Device Minor %d not accessible)",
                    gpu_params->gpuIds[i], device_minor);
        }
    }
    if (filtered_count == 0) {
        log_msg("WARNING: no accessible PCI GPU matched; original GPU list retained");
        return ret;
    }

    /* Write back the filtered list */
    for (int i = 0; i < filtered_count; i++) {
        gpu_params->gpuIds[i] = filtered[i];
    }
    for (int i = filtered_count; i < NV0000_CTRL_GPU_MAX_ATTACHED_GPUS; i++) {
        gpu_params->gpuIds[i] = NV0000_CTRL_GPU_INVALID_ID;
    }

    log_msg("filtered: %d -> %d GPUs", total_host_gpus, filtered_count);

    return ret;
}

/* Resolve immutable caches before FFmpeg starts its worker threads. */
__attribute__((constructor)) static void nvenc_fix_initialize(void) {
    ensure_real_ioctl();
    log_init();
    load_gpu_map();
}
