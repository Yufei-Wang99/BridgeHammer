/* Takes one argument: (float) amount of GiB you want to mlock
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <unistd.h>

#include "PageUtils.h"

#define GiB 1024ULL * 1024 * 1024

int main(int argc, char *argv[])
{
	if (argc != 2) {
		fprintf(stderr, "Usage: %s <GiB (float) to mlock>\n", argv[0]);
		return EXIT_FAILURE;
	}

	double nGiBs = atof(argv[1]);
	uint64_t bytes = (uint64_t)(GiB * nGiBs);

	uint64_t n_mapping_pages = bytes / page_size;

	char *map =
		mmap(NULL, bytes, PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

	if (map == MAP_FAILED) {
		perror("mmap");
		fprintf(stderr, "mmap %lu bytes failed\n", bytes);
		return EXIT_FAILURE;
	}

	// We set the first byte of every page to a random character.
	for (uint64_t i = 0; i < n_mapping_pages; ++i) {
		map[i * page_size] = ((i * 1000) % 26) + 'A';
	}

	if (mlock(map, bytes) == -1) {
		perror("mlock");
		munmap(map, bytes);
		return EXIT_FAILURE;
	}

	while (1) {
		sched_yield();
	}

	munmap(map, bytes);

	return EXIT_SUCCESS;
}