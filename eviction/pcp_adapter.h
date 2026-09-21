#ifndef PCP_ADAPTER_H
#define PCP_ADAPTER_H

#include <stddef.h>

/*
 * Hunt / drain stay in Python (page bank). This file is only:
 *   - CPU pin
 *   - file mmap that does not fault
 *   - critical section: release one hunt page, then fault the file page
 */
int pcp_pin_cpu(int cpu);
void *pcp_map_file_ro(const char *path, size_t *out_len);
int pcp_unmap(void *addr, size_t len);
/* Before the CS: VM_RAND_READ + file ra_pages=0. Not part of the CS. */
int pcp_advise_random(void *addr, size_t len);
int pcp_fadvise_random(const char *path);

/*
 * No malloc, printf, open, or threads. hunt_base and file_map must already
 * exist. file_off is the byte offset of the target page.
 */
int pcp_release_and_reload(void *hunt_base, size_t hunt_page_index,
			   void *file_map, size_t file_off, size_t page_size);

/*
 * hunt_idx is page-index order; file_off is fault order (high→low).
 * batch==0: old behavior (release all, then fault all). That overflows
 * this box's PCP (high≈621, often already half full) and scrambles LIFO.
 * batch>=1: release/fault in batches from the fault end (1 = one-by-one).
 */
int pcp_release_and_reload_n(void *hunt_base, const size_t *hunt_idx,
			     void *file_map, const size_t *file_off, size_t n,
			     size_t page_size, size_t batch);

#endif /* PCP_ADAPTER_H */
