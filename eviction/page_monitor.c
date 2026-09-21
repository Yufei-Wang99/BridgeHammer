#include "common.h"

static void emit_page(long page, int resident)
{
	printf("page=%ld,resident=%d\n", page, resident);
}

static int cmp_long_asc(const void *a, const void *b)
{
	long x = *(const long *)a;
	long y = *(const long *)b;
	if (x < y)
		return -1;
	if (x > y)
		return 1;
	return 0;
}

static int load_page_list(const char *path, long **out, int *n_out)
{
	FILE *fp = fopen(path, "r");
	if (!fp)
		return -1;

	int cap = 64;
	int n = 0;
	long *ps = malloc((size_t)cap * sizeof(long));
	char line[128];

	if (!ps) {
		fclose(fp);
		return -1;
	}
	while (fgets(line, sizeof(line), fp)) {
		char *s = line;
		while (*s == ' ' || *s == '\t')
			s++;
		if (*s == '\0' || *s == '#' || *s == '\n' || *s == '\r')
			continue;
		char *end = NULL;
		long v = strtol(s, &end, 10);
		if (end == s || v < 0) {
			free(ps);
			fclose(fp);
			return -1;
		}
		if (n == cap) {
			cap *= 2;
			long *np = realloc(ps, (size_t)cap * sizeof(long));
			if (!np) {
				free(ps);
				fclose(fp);
				return -1;
			}
			ps = np;
		}
		ps[n++] = v;
	}
	fclose(fp);
	if (n == 0) {
		free(ps);
		return -1;
	}
	qsort(ps, (size_t)n, sizeof(long), cmp_long_asc);
	int w = 0;
	for (int i = 0; i < n; i++) {
		if (w == 0 || ps[i] != ps[w - 1])
			ps[w++] = ps[i];
	}
	*out = ps;
	*n_out = w;
	return 0;
}

static int probe_and_emit(int fd, const long *pages, int n, size_t page_size,
			  int file_pages)
{
	int *res = calloc((size_t)n, sizeof(int));
	if (!res)
		return -1;

	/* Probe high -> low to reduce readahead pollution. */
	for (int i = n - 1; i >= 0; i--) {
		if (pages[i] >= file_pages) {
			fprintf(stderr, "page %ld out of range (file pages=%d)\n",
				pages[i], file_pages);
			free(res);
			return -1;
		}
		int r = ur_page_in_cache(fd, pages[i], page_size);
		if (r < 0) {
			perror("preadv2");
			free(res);
			return -1;
		}
		res[i] = r;
	}
	for (int i = 0; i < n; i++)
		emit_page(pages[i], res[i]);
	free(res);
	return 0;
}

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr,
			"Usage: %s <file> <page_index>\n"
			"       %s <file> --range <start> <count>\n"
			"       %s <file> --list <pages.txt>\n",
			argv[0], argv[0], argv[0]);
		return EXIT_FAILURE;
	}

	setvbuf(stdout, NULL, _IOLBF, 0);

	const char *path = argv[1];
	int list_mode = 0;
	const char *list_path = NULL;
	int range_mode = 0;
	long start = 0;
	long count = 0;
	long single = 0;

	if (strcmp(argv[2], "--range") == 0) {
		if (argc != 5) {
			fprintf(stderr, "Usage: %s <file> --range <start> <count>\n",
				argv[0]);
			return EXIT_FAILURE;
		}
		range_mode = 1;
		start = strtol(argv[3], NULL, 10);
		count = strtol(argv[4], NULL, 10);
		if (start < 0 || count <= 0) {
			fprintf(stderr, "invalid --range arguments\n");
			return EXIT_FAILURE;
		}
	} else if (strcmp(argv[2], "--list") == 0) {
		if (argc != 4) {
			fprintf(stderr, "Usage: %s <file> --list <pages.txt>\n",
				argv[0]);
			return EXIT_FAILURE;
		}
		list_mode = 1;
		list_path = argv[3];
	} else {
		if (argc != 3) {
			fprintf(stderr, "Usage: %s <file> <page_index>\n", argv[0]);
			return EXIT_FAILURE;
		}
		single = strtol(argv[2], NULL, 10);
		if (single < 0) {
			fprintf(stderr, "invalid page_index\n");
			return EXIT_FAILURE;
		}
		start = single;
		count = 1;
	}

	size_t page_size = ur_page_size();
	int n_pages = ur_file_pages(path, page_size, NULL);
	if (n_pages < 0) {
		perror("stat");
		return EXIT_FAILURE;
	}

	int fd = ur_open_readonly(path);
	if (fd < 0) {
		perror("open");
		return EXIT_FAILURE;
	}

	int rc = 0;
	if (list_mode) {
		long *pages = NULL;
		int n = 0;
		if (load_page_list(list_path, &pages, &n) != 0) {
			fprintf(stderr, "failed to read page list %s\n", list_path);
			close(fd);
			return EXIT_FAILURE;
		}
		rc = probe_and_emit(fd, pages, n, page_size, n_pages);
		free(pages);
		close(fd);
		return rc == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
	}

	if (start >= n_pages) {
		fprintf(stderr, "page %ld out of range (file pages=%d)\n", start,
			n_pages);
		close(fd);
		return EXIT_FAILURE;
	}

	long end = start + count;
	if (end > n_pages)
		end = n_pages;
	int n = (int)(end - start);
	long *pages = malloc((size_t)n * sizeof(long));
	if (!pages) {
		perror("malloc");
		close(fd);
		return EXIT_FAILURE;
	}
	for (int i = 0; i < n; i++)
		pages[i] = start + i;
	rc = probe_and_emit(fd, pages, n, page_size, n_pages);
	free(pages);
	close(fd);
	if (rc != 0)
		return EXIT_FAILURE;
	(void)range_mode;
	(void)single;
	return EXIT_SUCCESS;
}
