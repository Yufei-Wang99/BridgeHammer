#include "pcp_adapter.h"

#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

int pcp_pin_cpu(int cpu)
{
	cpu_set_t set;

	if (cpu < 0)
		return 0;
	CPU_ZERO(&set);
	CPU_SET((unsigned)cpu, &set);
	return sched_setaffinity(0, sizeof(set), &set);
}

void *pcp_map_file_ro(const char *path, size_t *out_len)
{
	int fd = open(path, O_RDONLY);
	struct stat st;
	void *map;

	if (fd < 0)
		return NULL;
	if (fstat(fd, &st) != 0) {
		close(fd);
		return NULL;
	}
	if (st.st_size <= 0) {
		close(fd);
		errno = EINVAL;
		return NULL;
	}
	map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
	close(fd);
	if (map == MAP_FAILED)
		return NULL;
	if (out_len)
		*out_len = (size_t)st.st_size;
	return map;
}

int pcp_unmap(void *addr, size_t len)
{
	if (!addr || addr == (void *)-1)
		return 0;
	return munmap(addr, len);
}

int pcp_advise_random(void *addr, size_t len)
{
	if (!addr || addr == (void *)-1 || len == 0)
		return -1;
	return madvise(addr, len, MADV_RANDOM);
}

int pcp_fadvise_random(const char *path)
{
	int fd;
	int rc;

	if (!path)
		return -1;
	fd = open(path, O_RDONLY);
	if (fd < 0)
		return -1;
	rc = posix_fadvise(fd, 0, 0, POSIX_FADV_RANDOM);
	close(fd);
	return rc;
}

int pcp_release_and_reload(void *hunt_base, size_t hunt_page_index,
			   void *file_map, size_t file_off, size_t page_size)
{
	char *hunt_page;
	volatile unsigned char sink;

	if (!hunt_base || !file_map || page_size == 0)
		return -1;
	hunt_page = (char *)hunt_base + hunt_page_index * page_size;
	if (madvise(hunt_page, page_size, MADV_DONTNEED) != 0)
		return -1;
	sink = *((volatile unsigned char *)file_map + file_off);
	(void)sink;
	return 0;
}

int pcp_release_and_reload_n(void *hunt_base, const size_t *hunt_idx,
			     void *file_map, const size_t *file_off, size_t n,
			     size_t page_size, size_t batch)
{
	size_t faulted = 0;
	size_t released = 0;
	volatile unsigned char sink = 0;

	if (!hunt_base || !file_map || !hunt_idx || !file_off || page_size == 0)
		return -1;
	if (n == 0)
		return 0;
	if (batch == 0 || batch > n)
		batch = n;

	while (faulted < n) {
		size_t k = batch;
		size_t start;
		size_t i;

		if (k > n - faulted)
			k = n - faulted;
		start = n - released - k;
		for (i = 0; i < k; i++) {
			char *hunt_page =
				(char *)hunt_base + hunt_idx[start + i] * page_size;
			if (madvise(hunt_page, page_size, MADV_DONTNEED) != 0)
				return -1;
		}
		released += k;
		for (i = 0; i < k; i++)
			sink ^= *((volatile unsigned char *)file_map +
				  file_off[faulted + i]);
		faulted += k;
	}
	(void)sink;
	return 0;
}
