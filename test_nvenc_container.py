"""Synthetic RM ioctl and FFmpeg wrapper tests; never use a real GPU."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nvenc_container", ROOT / "nvenc_container.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

HARNESS = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <sys/sysmacros.h>
#include <errno.h>
#define ioctl interposed_ioctl
#include "nvenc_fix.c"
#undef ioctl

static int backend_failure = 0, resolution_failure = 0;
static unsigned long valid_request(void) {
    return _IOC(_IOC_READ | _IOC_WRITE, 'F', 0x2A, 32);
}
int stat(const char *path, struct stat *s) {
    memset(s, 0, sizeof(*s));
    if (strcmp(path, "/dev/nvidia2") != 0) { errno = ENOENT; return -1; }
    s->st_mode = S_IFCHR | 0600; s->st_rdev = makedev(195, 2); return 0;
}
int fstat(int fd, struct stat *s) {
    memset(s, 0, sizeof(*s));
    if (fd != 123) { errno = EBADF; return -1; }
    s->st_mode = S_IFCHR | 0600; s->st_rdev = makedev(195, 255); return 0;
}
int open(const char *path, int flags, ...) {
    (void)flags;
    if (strcmp(path, "/dev/nvidia2") == 0) return 100;
    errno = ENOENT; return -1;
}
static int backend(int fd, unsigned long request, ...) {
    (void)fd;
    if (backend_failure) { errno = EIO; return -1; }
    if (request != valid_request()) return 0;
    va_list ap; va_start(ap, request);
    NVOS54_PARAMETERS *c = va_arg(ap, void *); va_end(ap);
    if (!c || c->cmd != 0x205) return 0;
    assert(c->paramsSize == 32);
    NV0000_CTRL_GPU_GET_ID_INFO_PARAMS *info = c->params;
    if (resolution_failure) { c->status = 0x1f; return 0; }
    info->deviceInstance = info->gpuId == 0x9400 ? 2 : 7;
    c->status = 0; return 0;
}
static void original(NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS *p) {
    for (int i=0; i<32; i++) p->gpuIds[i] = 0xffffffff;
    p->gpuIds[0] = 0x500; p->gpuIds[1] = 0x9400;
}
int main(void) {
    real_ioctl = backend;
    NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS p; original(&p);
    NVOS54_PARAMETERS c = {.hClient=1, .hObject=1, .cmd=0x201,
                           .params=&p, .paramsSize=128, .status=0};
    assert(interposed_ioctl(123, valid_request(), &c) == 0);
    assert(p.gpuIds[0] == 0x9400 && p.gpuIds[1] == 0xffffffff);
    puts("filter only the available character device: PASS");

    /* Success from another ioctl namespace must never dereference this pointer. */
    assert(interposed_ioctl(123, _IOC(_IOC_READ|_IOC_WRITE,'X',0x2A,32), (void*)1) == 0);
    assert(interposed_ioctl(123, _IOC(_IOC_READ|_IOC_WRITE,'F',0x2A,16), (void*)1) == 0);
    puts("wrong ioctl magic/size is untouched: PASS");

    original(&p); c.paramsSize=4;
    assert(interposed_ioctl(123, valid_request(), &c) == 0 && p.gpuIds[0] == 0x500);
    c.paramsSize=128; c.flags=4;
    assert(interposed_ioctl(123, valid_request(), &c) == 0 && p.gpuIds[0] == 0x500);
    c.flags=0;
    assert(interposed_ioctl(124, valid_request(), &c) == 0 && p.gpuIds[0] == 0x500);
    puts("wrong parameter ABI/flags/device is untouched: PASS");

    backend_failure=1; errno=0;
    assert(interposed_ioctl(123, valid_request(), &c) == -1 && errno == EIO);
    backend_failure=0;
    puts("kernel error and errno are preserved: PASS");

    gpu_map_loaded=1; gpu_map_count=2;
    gpu_map[0]=(gpu_proc_entry_t){.domain=0,.bus=0x94,.slot=0,.func=0,.device_minor=2};
    gpu_map[1]=(gpu_proc_entry_t){.domain=0,.bus=0x94,.slot=1,.func=0,.device_minor=7};
    assert(match_gpuid_to_minor(0x9400) == -1);
    puts("ambiguous PCI matching is refused: PASS");

    resolution_failure=1; gpu_map_count=0; original(&p);
    assert(interposed_ioctl(123, valid_request(), &c) == 0 && p.gpuIds[0] == 0x500
           && p.gpuIds[1] == 0x9400);
    puts("unknown mappings retain the original response: PASS");
    return 0;
}
'''


class NvencContainerTests(unittest.TestCase):
    def test_preload_is_scoped_to_the_ffmpeg_wrapper(self):
        with tempfile.TemporaryDirectory(prefix="nvenc-wrapper-") as directory:
            root = Path(directory)
            target = root / "fake ffmpeg"
            target.write_text('#!/bin/sh\nprintf "%s\\n" "$LD_PRELOAD" "$@"\n')
            target.chmod(0o755)
            wrapper = root / "wrapper"
            wrapper.write_text(helper.wrapper_text(Path("/tmp/owned-nvenc.so"), target))
            env = os.environ.copy()
            env.pop("LD_PRELOAD", None)
            before = os.environ.get("LD_PRELOAD")
            result = subprocess.run(["sh", str(wrapper), "arg with spaces", "--version"],
                                    env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(),
                             ["/tmp/owned-nvenc.so", "arg with spaces", "--version"])
            self.assertEqual(os.environ.get("LD_PRELOAD"), before)

    def test_preload_path_rejects_separators(self):
        for path in ("/tmp/with spaces/lib.so", "/tmp/a:b/lib.so"):
            with self.assertRaises(ValueError):
                helper.wrapper_text(Path(path), Path("/usr/bin/ffmpeg"))

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("gcc"),
                         "RM ABI and ELF tests require Linux GCC")
    def test_compiled_rm_boundary_and_gpu_filter(self):
        with tempfile.TemporaryDirectory(prefix="nvenc-rm-fixture-") as directory:
            root = Path(directory)
            shutil.copy2(ROOT / "nvenc_fix.c", root)
            (root / "harness.c").write_text(HARNESS)
            options = ["gcc", "-Wall", "-Wextra", "-Werror", "-O2"]
            subprocess.run(options + ["-shared", "-fPIC", "-o", str(root / "libfix.so"),
                                      str(root / "nvenc_fix.c"), "-ldl"], check=True,
                           capture_output=True, text=True)
            subprocess.run(options + ["-o", str(root / "harness"),
                                      str(root / "harness.c"), "-ldl"], check=True,
                           capture_output=True, text=True)
            result = subprocess.run([str(root / "harness")], check=True,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout.count("PASS"), 6, result.stdout)


if __name__ == "__main__":
    unittest.main()
