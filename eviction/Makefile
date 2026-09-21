CC := gcc
CFLAGS := -O2 -Wall -Wextra -std=c11
CPPFLAGS := -D_GNU_SOURCE

BUILD := build
PRIM := $(abspath $(CURDIR)/primitives)
UTIL := $(abspath $(CURDIR)/utilities)

OUR_BINS := \
	$(BUILD)/page_monitor \
	$(BUILD)/eviction_controller \
	$(BUILD)/scan_pagemap

WORKER_BINS := \
	$(BUILD)/evict-1-shlibs \
	$(BUILD)/evict-2-randaccess \
	$(BUILD)/evict-3-baseline \
	$(BUILD)/evict-4-dynpressure

.PHONY: all clean

all: $(OUR_BINS) $(WORKER_BINS) $(BUILD)/pcp_adapter.o $(BUILD)/libpcp_adapter.so

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/common.o: common.c common.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c common.c -o $@

$(BUILD)/pcp_adapter.o: pcp_adapter.c pcp_adapter.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c pcp_adapter.c -o $@

$(BUILD)/libpcp_adapter.so: pcp_adapter.c pcp_adapter.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -shared pcp_adapter.c -o $@

$(BUILD)/page_monitor: page_monitor.c $(BUILD)/common.o | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) page_monitor.c $(BUILD)/common.o -o $@

$(BUILD)/eviction_controller: eviction_controller.c $(BUILD)/common.o | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) eviction_controller.c $(BUILD)/common.o -o $@

$(BUILD)/scan_pagemap: scan_pagemap.c | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) scan_pagemap.c -o $@

WORKER_CFLAGS := -O2 -Wall -Wextra -pedantic -Wno-unused-parameter
WORKER_INCLUDES := -I$(PRIM)/include -I$(UTIL)/include
WORKER_SRCS := $(PRIM)/utils/GeneralUtils.c $(UTIL)/AlignedPage.c

$(BUILD)/evict-1-shlibs: $(PRIM)/evict/evict-1-shlibs.c $(WORKER_SRCS) | $(BUILD)
	$(CC) $(PRIM)/evict/evict-1-shlibs.c $(WORKER_SRCS) $(WORKER_CFLAGS) \
		$(WORKER_INCLUDES) -o $@

$(BUILD)/evict-2-randaccess: $(PRIM)/evict/evict-2-randaccess.c $(WORKER_SRCS) | $(BUILD)
	$(CC) $(PRIM)/evict/evict-2-randaccess.c $(WORKER_SRCS) $(WORKER_CFLAGS) \
		$(WORKER_INCLUDES) -o $@

$(BUILD)/evict-3-baseline: $(PRIM)/evict/evict-3-baseline.c $(WORKER_SRCS) | $(BUILD)
	$(CC) $(PRIM)/evict/evict-3-baseline.c $(WORKER_SRCS) $(WORKER_CFLAGS) \
		$(WORKER_INCLUDES) -o $@

$(BUILD)/evict-4-dynpressure: $(PRIM)/evict/evict-4-dynpressure.c $(WORKER_SRCS) | $(BUILD)
	$(CC) $(PRIM)/evict/evict-4-dynpressure.c $(WORKER_SRCS) $(WORKER_CFLAGS) \
		-pthread $(WORKER_INCLUDES) -o $@

clean:
	rm -rf $(BUILD)
