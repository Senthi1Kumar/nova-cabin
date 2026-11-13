/* nova-map-service: control lines on a Unix socket, RGBA frames in shared memory.
   MapLibre sources stay in drive-os and are referenced from CMake. They are not copied. */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

struct frame_header {
    uint32_t seq, w, h, stride, flags, reserved;
};

int main(int argc, char **argv) {
    const char *shm_path = "/nova-map";
    const char *sock_path = NULL;
    uint32_t w = 1280, h = 720;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--shm") && i + 1 < argc) shm_path = argv[++i];
        else if (!strcmp(argv[i], "--sock") && i + 1 < argc) sock_path = argv[++i];
        else if (!strcmp(argv[i], "--w") && i + 1 < argc) w = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--h") && i + 1 < argc) h = (uint32_t)atoi(argv[++i]);
    }
    if (!sock_path) {
        fprintf(stderr, "nova-map-service: --sock is required\n");
        return 2;
    }
    uint32_t stride = w * 4;
    size_t bytes = sizeof(struct frame_header) + (size_t)stride * h;
    int fd = open(shm_path, O_RDWR | O_CREAT, 0600);
    if (fd < 0 || ftruncate(fd, (off_t)bytes) != 0) return 1;
    struct frame_header *hdr = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (hdr == MAP_FAILED) return 1;
    hdr->w = w;
    hdr->h = h;
    hdr->stride = stride;
    hdr->seq = 1;
    fprintf(stderr, "nova-map-service: shm ready %ux%u. Live MapLibre stays off until the thermal gate passes.\n", w, h);
    pause();
    munmap(hdr, bytes);
    close(fd);
    return 0;
}
