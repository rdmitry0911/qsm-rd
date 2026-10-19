#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
int MFXVideoCORE_SyncOperation(void *session, void *point, unsigned wait) {
    static int (*next)(void *, void *, unsigned);
    if (!next) next = dlsym(RTLD_NEXT, "MFXVideoCORE_SyncOperation");
    if (!next) return -3;
    int result = next(session, point, wait);
    if (result < 0) fprintf(stderr, "QSM_PROBE_MFX_SYNC_STATUS=%d\n", result);
    return result;
}
