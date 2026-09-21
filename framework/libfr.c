#define _GNU_SOURCE

#include <signal.h>
#include <stdint.h>
#include <unistd.h>

static inline uint64_t rdtsc_now(void)
{
	uint32_t lo, hi;
	asm volatile("rdtsc" : "=a"(lo), "=d"(hi));
	return ((uint64_t)hi << 32) | lo;
}

void fr_flush(void *p)
{
	asm volatile("clflushopt (%0)\n\tmfence" ::"r"(p) : "memory");
}

uint32_t fr_flush_reload_time(void *p, int pause_iters)
{
	volatile int i;
	uint64_t t0, t1;
	volatile unsigned char x;

	fr_flush(p);
	for (i = 0; i < pause_iters; i++) {
	}
	t0 = rdtsc_now();
	asm volatile("" ::: "memory");
	x = *(volatile unsigned char *)p;
	(void)x;
	t1 = rdtsc_now();
	fr_flush(p);
	return (uint32_t)(t1 - t0);
}

uint64_t fr_monitor_line_sigstop(void *p, uint32_t threshold, uint64_t max_samples,
				 int pause_iters, int min_consecutive, int pid,
				 uint32_t *best, uint64_t *elapsed, int *kill_rc)
{
	uint64_t i = 0;
	int streak = 0;
	uint32_t best_c = 0xffffffffu;
	uint64_t t0 = rdtsc_now();

	for (;;) {
		uint32_t c;

		if (max_samples && i >= max_samples)
			break;
		c = fr_flush_reload_time(p, pause_iters);
		if (c < best_c)
			best_c = c;
		if (c < threshold) {
			streak++;
			if (streak >= min_consecutive) {
				if (kill_rc)
					*kill_rc = kill(pid, SIGSTOP);
				if (best)
					*best = best_c;
				if (elapsed)
					*elapsed = rdtsc_now() - t0;
				return i + 1;
			}
		} else {
			streak = 0;
		}
		i++;
	}
	if (best)
		*best = best_c;
	if (elapsed)
		*elapsed = rdtsc_now() - t0;
	if (kill_rc)
		*kill_rc = -1;
	return 0;
}
