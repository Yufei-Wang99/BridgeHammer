#include "common.h"

size_t ur_page_size(void)
{
	long ps = sysconf(_SC_PAGESIZE);
	if (ps <= 0)
		return 4096;
	return (size_t)ps;
}

long ur_memavailable_kb(void)
{
	FILE *fp = fopen("/proc/meminfo", "r");
	if (!fp)
		return -1;

	char line[256];
	long value = -1;
	while (fgets(line, sizeof(line), fp)) {
		if (sscanf(line, "MemAvailable: %ld kB", &value) == 1)
			break;
	}
	fclose(fp);
	return value;
}

int ur_file_pages(const char *path, size_t page_size, off_t *file_size_out)
{
	struct stat st;
	if (stat(path, &st) != 0)
		return -1;
	if (file_size_out)
		*file_size_out = st.st_size;
	if (page_size == 0)
		return -1;
	if (st.st_size == 0)
		return 0;
	return (int)((st.st_size + (off_t)page_size - 1) / (off_t)page_size);
}

int ur_page_in_cache(int fd, long page_index, size_t page_size)
{
	char buffer = 0;
	struct iovec iov;
	iov.iov_base = &buffer;
	iov.iov_len = 1;

	ssize_t ret = preadv2(fd, &iov, 1, (off_t)page_index * (off_t)page_size,
			      RWF_NOWAIT);
	if (ret < 0) {
		if (errno == EAGAIN)
			return 0;
		return -1;
	}
	if (ret == 1)
		return 1;
	return -1;
}

int ur_open_readonly(const char *path)
{
	return open(path, O_RDONLY);
}
