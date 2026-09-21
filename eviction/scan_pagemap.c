#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc != 4) {
    fprintf(stderr, "usage: scan_pagemap <pid> <vaddr> <pages>\n");
    return 2;
  }
  const int pid = atoi(argv[1]);
  const unsigned long va = strtoul(argv[2], NULL, 0);
  const int pages = atoi(argv[3]);
  if (pid <= 0 || pages <= 0) {
    return 2;
  }

  char path[64];
  snprintf(path, sizeof path, "/proc/%d/pagemap", pid);
  const int fd = open(path, O_RDONLY);
  if (fd < 0) {
    perror(path);
    return 1;
  }

  const long ps = sysconf(_SC_PAGESIZE);
  if (lseek(fd, (off_t)((va / (unsigned long)ps) * 8UL), SEEK_SET) < 0) {
    perror("lseek");
    close(fd);
    return 1;
  }

  for (int i = 0; i < pages; i++) {
    uint64_t entry = 0;
    if (read(fd, &entry, sizeof entry) != (ssize_t)sizeof entry) {
      puts("0");
      continue;
    }
    const uint64_t pfn = (entry & (1ULL << 63)) ? (entry & ((1ULL << 55) - 1)) : 0;
    printf("%llu\n", (unsigned long long)pfn);
  }
  close(fd);
  return 0;
}
