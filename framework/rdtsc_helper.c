/*
 * rdtsc_helper.c — Serialized RDTSC timing for precise cycle-level measurement.
 *
 * Two functions following Intel's recommended practice:
 *   rdtsc_start(): lfence; rdtsc  — drain pipeline BEFORE reading counter
 *   rdtsc_end():   rdtscp; lfence — read counter then drain pipeline AFTER
 *
 * Build:
 *   gcc -O2 -shared -fPIC -o librdtsc.so rdtsc_helper.c
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

uint64_t rdtsc_start(void) {
    uint32_t lo, hi;
    __asm__ __volatile__(
        "lfence\n\t"
        "rdtsc"
        : "=a"(lo), "=d"(hi)
    );
    return ((uint64_t)hi << 32) | lo;
}

uint64_t rdtsc_end(void) {
    uint32_t lo, hi, aux;
    __asm__ __volatile__(
        "rdtscp"
        : "=a"(lo), "=d"(hi), "=c"(aux)
    );
    __asm__ __volatile__("lfence");
    return ((uint64_t)hi << 32) | lo;
}

/*
 * Return the invariant TSC frequency in kHz.
 *
 * Try in order:
 *   1. /sys/devices/system/cpu/cpu0/tsc_freq_khz   (kernel >= 4.12)
 *   2. Parse base clock from /proc/cpuinfo "model name" line (e.g. "@ 3.20GHz")
 *   3. Fall back to current "cpu MHz" (less accurate, varies with turbo)
 */
uint64_t cpu_freq_khz(void) {
    FILE *f;
    char buf[256];

    /* Method 1: sysfs (most accurate) */
    f = fopen("/sys/devices/system/cpu/cpu0/tsc_freq_khz", "r");
    if (f) {
        uint64_t khz = 0;
        if (fscanf(f, "%lu", &khz) == 1 && khz > 0) {
            fclose(f);
            return khz;
        }
        fclose(f);
    }

    /* Method 2: base clock from model name (invariant TSC rate) */
    f = fopen("/proc/cpuinfo", "r");
    if (f) {
        while (fgets(buf, sizeof(buf), f)) {
            char *at = strstr(buf, "@ ");
            if (at) {
                double ghz = 0.0;
                if (sscanf(at + 2, "%lfGHz", &ghz) == 1 && ghz > 0) {
                    fclose(f);
                    return (uint64_t)(ghz * 1e6);  /* GHz → kHz */
                }
            }
        }
        /* Method 3: current cpu MHz (fallback) */
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
