#ifndef UNPRIV_COMMON_H
#define UNPRIV_COMMON_H

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>

#ifndef RWF_NOWAIT
#define RWF_NOWAIT 0x00000008
#endif

#define UR_PRESENT_BIT (1ULL << 63)
#define UR_PFN_MASK ((1ULL << 55) - 1)

size_t ur_page_size(void);
long ur_memavailable_kb(void);
int ur_file_pages(const char *path, size_t page_size, off_t *file_size_out);
int ur_page_in_cache(int fd, long page_index, size_t page_size);
int ur_open_readonly(const char *path);

#endif /* UNPRIV_COMMON_H */
