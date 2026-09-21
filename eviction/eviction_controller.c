#include "common.h"

#include <ctype.h>
#include <dirent.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>

#define MAX_CHILDREN 16
#define MAX_SHLIBS 4096
#define MAX_SHLIB_PATH 512

struct opts {
	const char *file;
	long page_index;
	int timeout_s;
	int poll_ms;
	int use_shlibs;
	int use_randaccess;
	int use_dynpressure;
	int use_baseline;
	int until_all;
	int until_pages;
	int baseline_count;
	double baseline_gib;
	int n_pages;
	char bin_dir[PATH_MAX];
	char page_monitor[PATH_MAX];
	char list_shlibs[PATH_MAX];
	char until_pages_path[PATH_MAX];
	int page_monitor_set;
};

static pid_t g_children[MAX_CHILDREN];
static int g_nchildren;
static pid_t g_pgid = -1;

static uint64_t now_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

static void join_path(char *out, size_t n, const char *dir, const char *name)
{
	snprintf(out, n, "%s/%s", dir, name);
}

static int file_exists(const char *path)
{
	return access(path, X_OK) == 0;
}

static void dirname_of(const char *path, char *out, size_t n)
{
	snprintf(out, n, "%s", path);
	char *slash = strrchr(out, '/');
	if (!slash) {
		snprintf(out, n, ".");
		return;
	}
	if (slash == out) {
		slash[1] = '\0';
		return;
	}
	*slash = '\0';
}

static void sleep_ms(int ms)
{
	struct timespec ts;
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (long)(ms % 1000) * 1000000L;
	nanosleep(&ts, NULL);
}

static int spawn_in_group(const char *path, char *const argv[])
{
	if (g_nchildren >= MAX_CHILDREN)
		return -1;

	pid_t pid = fork();
	if (pid < 0) {
		perror("fork");
		return -1;
	}
	if (pid == 0) {
		if (g_pgid > 0)
			setpgid(0, g_pgid);
		else
			setpgid(0, 0);

		int devnull = open("/dev/null", O_WRONLY);
		if (devnull >= 0) {
			dup2(devnull, STDOUT_FILENO);
			dup2(devnull, STDERR_FILENO);
			if (devnull > 2)
				close(devnull);
		}
		execv(path, argv);
		_exit(127);
	}

	if (g_pgid <= 0) {
		if (setpgid(pid, pid) != 0 && errno != EACCES)
			perror("setpgid");
		g_pgid = pid;
	} else {
		if (setpgid(pid, g_pgid) != 0 && errno != EACCES)
			perror("setpgid");
	}

	g_children[g_nchildren++] = pid;
	return 0;
}

static void stop_workers(void)
{
	if (g_pgid > 0) {
		kill(-g_pgid, SIGTERM);
		sleep_ms(200);
		kill(-g_pgid, SIGKILL);
	}

	for (int i = 0; i < g_nchildren; i++) {
		if (g_children[i] > 0) {
			int status;
			waitpid(g_children[i], &status, 0);
			g_children[i] = -1;
		}
	}
}

static int monitor_range_resident_count(const char *monitor, const char *file,
					int n_pages)
{
	if (n_pages <= 0)
		return -1;

	char cmd[PATH_MAX * 2];
	snprintf(cmd, sizeof(cmd), "\"%s\" \"%s\" --range 0 %d", monitor, file,
		 n_pages);

	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;

	char line[128];
	int resident_count = 0;
	int seen = 0;
	while (fgets(line, sizeof(line), fp)) {
		long p = -1;
		int r = -1;
		if (sscanf(line, "page=%ld,resident=%d", &p, &r) != 2)
			continue;
		seen++;
		if (r == 1)
			resident_count++;
	}
	pclose(fp);
	if (seen == 0)
		return -1;
	return resident_count;
}

static int monitor_list_resident_count(const char *monitor, const char *file,
				       const char *list_path)
{
	char cmd[PATH_MAX * 3];
	snprintf(cmd, sizeof(cmd), "\"%s\" \"%s\" --list \"%s\"", monitor, file,
		 list_path);

	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;

	char line[128];
	int resident_count = 0;
	int seen = 0;
	while (fgets(line, sizeof(line), fp)) {
		long p = -1;
		int r = -1;
		if (sscanf(line, "page=%ld,resident=%d", &p, &r) != 2)
			continue;
		seen++;
		if (r == 1)
			resident_count++;
	}
	pclose(fp);
	if (seen == 0)
		return -1;
	return resident_count;
}

static int monitor_resident(const char *monitor, const char *file, long page)
{
	char cmd[PATH_MAX * 2];
	snprintf(cmd, sizeof(cmd), "\"%s\" \"%s\" %ld", monitor, file, page);

	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;

	char line[128];
	int resident = -1;
	while (fgets(line, sizeof(line), fp)) {
		long p = -1;
		int r = -1;
		if (sscanf(line, "page=%ld,resident=%d", &p, &r) == 2 && p == page)
			resident = r;
	}
	int rc = pclose(fp);
	if (rc != 0 && resident < 0)
		return -1;
	return resident;
}

static int collect_shlibs_from_script(const char *script, char ***out_argv,
				      int *out_n)
{
	char cmd[PATH_MAX + 16];
	snprintf(cmd, sizeof(cmd), "\"%s\"", script);
	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;

	char buf[1 << 20];
	size_t nread = fread(buf, 1, sizeof(buf) - 1, fp);
	buf[nread] = '\0';
	pclose(fp);

	char **argv = calloc(MAX_SHLIBS + 2, sizeof(char *));
	if (!argv)
		return -1;

	int n = 0;
	char *save = NULL;
	for (char *tok = strtok_r(buf, " \t\n", &save); tok;
	     tok = strtok_r(NULL, " \t\n", &save)) {
		if (n >= MAX_SHLIBS)
			break;
		if (access(tok, R_OK) != 0)
			continue;
		argv[n] = strdup(tok);
		if (!argv[n])
			break;
		n++;
	}
	*out_argv = argv;
	*out_n = n;
	return 0;
}

static int add_unique_path(char **paths, int *n, const char *path)
{
	if (*n >= MAX_SHLIBS)
		return 0;
	if (access(path, R_OK) != 0)
		return 0;
	for (int i = 0; i < *n; i++) {
		if (strcmp(paths[i], path) == 0)
			return 0;
	}
	paths[*n] = strdup(path);
	if (!paths[*n])
		return -1;
	(*n)++;
	return 0;
}

static int collect_shlibs_from_proc(char ***out_argv, int *out_n)
{
	char **paths = calloc(MAX_SHLIBS + 2, sizeof(char *));
	if (!paths)
		return -1;
	int n = 0;

	DIR *proc = opendir("/proc");
	if (!proc) {
		free(paths);
		return -1;
	}

	struct dirent *de;
	while ((de = readdir(proc)) != NULL) {
		if (!isdigit((unsigned char)de->d_name[0]))
			continue;
		char maps[PATH_MAX];
		snprintf(maps, sizeof(maps), "/proc/%s/maps", de->d_name);
		FILE *fp = fopen(maps, "r");
		if (!fp)
			continue;
		char line[4096];
		while (fgets(line, sizeof(line), fp)) {
			char *so = strstr(line, ".so.");
			if (!so)
				continue;
			char *path = strchr(line, '/');
			if (!path)
				continue;
			size_t len = strlen(path);
			while (len > 0 && (path[len - 1] == '\n' || path[len - 1] == '\r')) {
				path[len - 1] = '\0';
				len--;
			}
			if (add_unique_path(paths, &n, path) < 0) {
				fclose(fp);
				closedir(proc);
				*out_argv = paths;
				*out_n = n;
				return -1;
			}
		}
		fclose(fp);
	}
	closedir(proc);
	*out_argv = paths;
	*out_n = n;
	return 0;
}

static void free_strv(char **v, int n)
{
	if (!v)
		return;
	for (int i = 0; i < n; i++)
		free(v[i]);
	free(v);
}

static int start_shlibs(const struct opts *opt)
{
	char path[PATH_MAX];
	join_path(path, sizeof(path), opt->bin_dir, "evict-1-shlibs");
	if (!file_exists(path)) {
		fprintf(stderr, "missing %s\n", path);
		return -1;
	}

	char **libs = NULL;
	int nlibs = 0;
	if (opt->list_shlibs[0] && access(opt->list_shlibs, X_OK) == 0) {
		if (collect_shlibs_from_script(opt->list_shlibs, &libs, &nlibs) != 0)
			nlibs = 0;
	}
	if (nlibs == 0) {
		free_strv(libs, nlibs);
		libs = NULL;
		if (collect_shlibs_from_proc(&libs, &nlibs) != 0)
			nlibs = 0;
	}
	if (nlibs == 0) {
		fprintf(stderr, "no shared libraries found for evict-1-shlibs\n");
		free_strv(libs, nlibs);
		return -1;
	}

	char **argv = calloc((size_t)nlibs + 2, sizeof(char *));
	if (!argv) {
		free_strv(libs, nlibs);
		return -1;
	}
	argv[0] = path;
	for (int i = 0; i < nlibs; i++)
		argv[i + 1] = libs[i];
	argv[nlibs + 1] = NULL;

	int rc = spawn_in_group(path, argv);
	free(argv);
	free_strv(libs, nlibs);
	return rc;
}

static int start_named(const struct opts *opt, const char *name)
{
	char path[PATH_MAX];
	join_path(path, sizeof(path), opt->bin_dir, name);
	if (!file_exists(path)) {
		fprintf(stderr, "missing %s\n", path);
		return -1;
	}
	char *argv[] = { path, NULL };
	return spawn_in_group(path, argv);
}

static int start_baseline(const struct opts *opt)
{
	char path[PATH_MAX];
	join_path(path, sizeof(path), opt->bin_dir, "evict-3-baseline");
	if (!file_exists(path)) {
		fprintf(stderr, "missing %s\n", path);
		return -1;
	}

	char gib[32];
	snprintf(gib, sizeof(gib), "%.1f", opt->baseline_gib);
	int started = 0;
	for (int i = 0; i < opt->baseline_count; i++) {
		char *argv[] = { path, gib, NULL };
		if (spawn_in_group(path, argv) != 0) {
			fprintf(stderr, "warning: failed to start evict-3-baseline %d\n",
				i);
			continue;
		}
		started++;
		if (i + 1 < opt->baseline_count)
			sleep_ms(500);
	}
	return started > 0 ? 0 : -1;
}

static double auto_baseline_gib(void)
{
	long mem_kb = ur_memavailable_kb();
	if (mem_kb <= 0)
		return 0.4;
	double gib = (double)mem_kb / (8.0 * 1000.0 * 1000.0);
	if (gib < 0.1)
		gib = 0.1;
	return gib;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s <file> <page_index> [--timeout-s N] [--workers LIST]\n"
		"         [--bin-dir DIR] [--page-monitor PATH] [--list-shlibs PATH]\n"
		"         [--poll-ms N] [--until-all] [--until-pages FILE]\n"
		"         [--baseline-gib G] [--baseline-count N]\n"
		"Workers: shlibs,randaccess,baseline,dynpressure\n"
		"  baseline = evict-3-baseline (mlock), default 7 copies\n"
		"  dynpressure = evict-4-dynpressure (mlock)\n",
		prog);
}

static int parse_workers(struct opts *opt, const char *list)
{
	opt->use_shlibs = 0;
	opt->use_randaccess = 0;
	opt->use_dynpressure = 0;
	opt->use_baseline = 0;
	char buf[256];
	snprintf(buf, sizeof(buf), "%s", list);
	char *save = NULL;
	for (char *tok = strtok_r(buf, ",", &save); tok;
	     tok = strtok_r(NULL, ",", &save)) {
		if (strcmp(tok, "shlibs") == 0)
			opt->use_shlibs = 1;
		else if (strcmp(tok, "randaccess") == 0)
			opt->use_randaccess = 1;
		else if (strcmp(tok, "dynpressure") == 0)
			opt->use_dynpressure = 1;
		else if (strcmp(tok, "baseline") == 0)
			opt->use_baseline = 1;
		else {
			fprintf(stderr, "unknown worker '%s'\n", tok);
			return -1;
		}
	}
	return 0;
}

static int parse_args(int argc, char **argv, struct opts *opt)
{
	memset(opt, 0, sizeof(*opt));
	opt->timeout_s = 90;
	opt->poll_ms = 50;
	opt->use_shlibs = 1;
	opt->use_randaccess = 1;
	opt->baseline_count = 7;
	opt->baseline_gib = 0.0;

	if (argc < 3) {
		usage(argv[0]);
		return -1;
	}
	opt->file = argv[1];
	opt->page_index = strtol(argv[2], NULL, 10);
	if (opt->page_index < 0) {
		fprintf(stderr, "invalid page_index\n");
		return -1;
	}

	char self[PATH_MAX];
	ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
	if (n > 0) {
		self[n] = '\0';
		dirname_of(self, opt->bin_dir, sizeof(opt->bin_dir));
	} else {
		snprintf(opt->bin_dir, sizeof(opt->bin_dir), ".");
	}
	join_path(opt->page_monitor, sizeof(opt->page_monitor), opt->bin_dir,
		  "page_monitor");

	for (int i = 3; i < argc; i++) {
		if (strcmp(argv[i], "--timeout-s") == 0 && i + 1 < argc) {
			opt->timeout_s = atoi(argv[++i]);
		} else if (strcmp(argv[i], "--poll-ms") == 0 && i + 1 < argc) {
			opt->poll_ms = atoi(argv[++i]);
		} else if (strcmp(argv[i], "--workers") == 0 && i + 1 < argc) {
			if (parse_workers(opt, argv[++i]) != 0)
				return -1;
		} else if (strcmp(argv[i], "--bin-dir") == 0 && i + 1 < argc) {
			snprintf(opt->bin_dir, sizeof(opt->bin_dir), "%s", argv[++i]);
		} else if (strcmp(argv[i], "--page-monitor") == 0 && i + 1 < argc) {
			snprintf(opt->page_monitor, sizeof(opt->page_monitor), "%s",
				 argv[++i]);
			opt->page_monitor_set = 1;
		} else if (strcmp(argv[i], "--list-shlibs") == 0 && i + 1 < argc) {
			snprintf(opt->list_shlibs, sizeof(opt->list_shlibs), "%s",
				 argv[++i]);
		} else if (strcmp(argv[i], "--until-all") == 0) {
			opt->until_all = 1;
		} else if (strcmp(argv[i], "--until-pages") == 0 && i + 1 < argc) {
			snprintf(opt->until_pages_path, sizeof(opt->until_pages_path),
				 "%s", argv[++i]);
			opt->until_pages = 1;
		} else if (strcmp(argv[i], "--baseline-gib") == 0 && i + 1 < argc) {
			opt->baseline_gib = atof(argv[++i]);
		} else if (strcmp(argv[i], "--baseline-count") == 0 && i + 1 < argc) {
			opt->baseline_count = atoi(argv[++i]);
		} else {
			fprintf(stderr, "unknown argument: %s\n", argv[i]);
			usage(argv[0]);
			return -1;
		}
	}

	if (!opt->page_monitor_set)
		join_path(opt->page_monitor, sizeof(opt->page_monitor), opt->bin_dir,
			  "page_monitor");
	if (opt->timeout_s <= 0)
		opt->timeout_s = 90;
	if (opt->poll_ms <= 0)
		opt->poll_ms = 50;
	if (opt->baseline_count <= 0)
		opt->baseline_count = 7;
	if (opt->baseline_count > MAX_CHILDREN - 3)
		opt->baseline_count = MAX_CHILDREN - 3;
	if (!opt->use_shlibs && !opt->use_randaccess && !opt->use_dynpressure &&
	    !opt->use_baseline) {
		fprintf(stderr, "no workers selected\n");
		return -1;
	}
	if (opt->until_all && opt->until_pages) {
		fprintf(stderr, "--until-all and --until-pages are mutually exclusive\n");
		return -1;
	}
	if (opt->until_pages && access(opt->until_pages_path, R_OK) != 0) {
		fprintf(stderr, "until-pages not readable: %s\n",
			opt->until_pages_path);
		return -1;
	}
	if (access(opt->page_monitor, X_OK) != 0) {
		fprintf(stderr, "page_monitor not executable: %s\n",
			opt->page_monitor);
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct opts opt;
	if (parse_args(argc, argv, &opt) != 0)
		return EXIT_FAILURE;

	setvbuf(stdout, NULL, _IOLBF, 0);

	opt.n_pages = ur_file_pages(opt.file, ur_page_size(), NULL);
	if (opt.n_pages < 0) {
		perror("stat");
		return EXIT_FAILURE;
	}
	if ((opt.until_all || opt.until_pages) && opt.n_pages == 0) {
		fprintf(stderr, "file has zero pages\n");
		return EXIT_FAILURE;
	}

	long mem_before = ur_memavailable_kb();
	if (opt.use_baseline && opt.baseline_gib <= 0.0)
		opt.baseline_gib = auto_baseline_gib();

	uint64_t t0 = now_us();
	int success = 0;
	uint64_t latency = 0;
	int pages_in_cache = -1;

	if (opt.use_baseline && start_baseline(&opt) != 0)
		fprintf(stderr, "warning: failed to start evict-3-baseline\n");
	if (opt.use_shlibs && start_shlibs(&opt) != 0)
		fprintf(stderr, "warning: failed to start evict-1-shlibs\n");
	if (opt.use_randaccess && start_named(&opt, "evict-2-randaccess") != 0)
		fprintf(stderr, "warning: failed to start evict-2-randaccess\n");
	if (opt.use_dynpressure && start_named(&opt, "evict-4-dynpressure") != 0)
		fprintf(stderr, "warning: failed to start evict-4-dynpressure\n");

	if (g_nchildren == 0) {
		fprintf(stderr, "no eviction workers started\n");
		long mem_after = ur_memavailable_kb();
		printf("eviction_success=0\n");
		printf("eviction_latency_us=0\n");
		printf("memavailable_before_kb=%ld\n", mem_before);
		printf("memavailable_after_kb=%ld\n", mem_after);
		return EXIT_FAILURE;
	}

	uint64_t deadline = t0 + (uint64_t)opt.timeout_s * 1000000ULL;
	while (now_us() < deadline) {
		if (opt.until_pages) {
			pages_in_cache = monitor_list_resident_count(
				opt.page_monitor, opt.file, opt.until_pages_path);
			if (pages_in_cache == 0) {
				success = 1;
				latency = now_us() - t0;
				break;
			}
		} else if (opt.until_all) {
			pages_in_cache = monitor_range_resident_count(
				opt.page_monitor, opt.file, opt.n_pages);
			if (pages_in_cache == 0) {
				success = 1;
				latency = now_us() - t0;
				break;
			}
		} else {
			int resident = monitor_resident(opt.page_monitor, opt.file,
							opt.page_index);
			if (resident == 0) {
				success = 1;
				latency = now_us() - t0;
				break;
			}
		}
		sleep_ms(opt.poll_ms);
	}

	if (!success)
		latency = now_us() - t0;

	stop_workers();
	long mem_after = ur_memavailable_kb();

	if ((opt.until_all || opt.until_pages) && pages_in_cache < 0) {
		if (opt.until_pages)
			pages_in_cache = monitor_list_resident_count(
				opt.page_monitor, opt.file, opt.until_pages_path);
		else
			pages_in_cache = monitor_range_resident_count(
				opt.page_monitor, opt.file, opt.n_pages);
	}

	printf("eviction_success=%d\n", success);
	printf("eviction_latency_us=%" PRIu64 "\n", latency);
	printf("memavailable_before_kb=%ld\n", mem_before);
	printf("memavailable_after_kb=%ld\n", mem_after);
	if (opt.use_baseline)
		printf("baseline_gib=%.2f\n", opt.baseline_gib);
	if (opt.until_pages)
		printf("until_mode=targets\n");
	else if (opt.until_all)
		printf("until_mode=all\n");
	else
		printf("until_mode=single\n");
	if (opt.until_all || opt.until_pages)
		printf("pages_in_cache=%d\n", pages_in_cache);
	return success ? EXIT_SUCCESS : EXIT_FAILURE;
}
