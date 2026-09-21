.PHONY: all eviction fr hammer clean

all: eviction fr

eviction:
	$(MAKE) -C eviction

fr:
	gcc -O2 -shared -fPIC -o framework/libfr.so framework/libfr.c

hammer:
	cmake -S blacksmith -B blacksmith/build
	cmake --build blacksmith/build --target blacksmith_py -j$$(nproc)

clean:
	$(MAKE) -C eviction clean
	rm -f framework/libfr.so framework/librdtsc.so
	rm -rf blacksmith/build
