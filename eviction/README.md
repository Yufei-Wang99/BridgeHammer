# Eviction and PCP

Page-cache pressure until target file pages are cold (`preadv2(RWF_NOWAIT)`), then `madvise(MADV_DONTNEED)` on held frames and an immediate fault of the same file offsets.

```bash
make -C eviction
```
