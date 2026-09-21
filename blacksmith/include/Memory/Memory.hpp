/*
 * Copyright (c) 2021 by ETH Zurich.
 * Licensed under the MIT License, see LICENSE file for more details.
 */

#ifndef BLACKSMITH_SRC_MEMORY_H_
#define BLACKSMITH_SRC_MEMORY_H_

#include <cstdint>
#include <cstdlib>
#include <string>
#include <utility>
#include <vector>

#include "Memory/DramAnalyzer.hpp"
#include "Fuzzer/PatternAddressMapper.hpp"

enum class DATA_PATTERN : char {
  ZEROES, ONES, RANDOM
};

class Memory {
 private:
  volatile char *start_address = nullptr;

  // First mapping size (contiguous range used by check_memory).
  uint64_t size;

  // Ignored: allocation is always 4KB pages, never hugepage/superpage.
  const bool superpage;

  struct Region {
    volatile char *addr;
    size_t nbytes;
  };
  std::vector<Region> regions;

  bool map_anonymous(size_t nbytes, Region *out);

  size_t check_memory_internal(PatternAddressMapper &mapping, const volatile char *start,
                               const volatile char *end, bool reproducibility_mode, bool verbose);

 public:

  // the flipped bits detected during the last call to check_memory
  std::vector<BitFlip> flipped_bits;

  explicit Memory(bool use_superpage);

  ~Memory();

  void allocate_memory(size_t mem_size);

  void catalog_allocated_pages();

  // Grow the 4KB spray until each (bank,row) is present or max_bytes is reached.
  bool ensure_rows(const std::vector<std::pair<size_t, size_t>> &bank_rows, size_t max_bytes);

  void initialize(DATA_PATTERN data_pattern);

  size_t check_memory(const volatile char *start, const volatile char *end);

  size_t check_memory(PatternAddressMapper &mapping, bool reproducibility_mode, bool verbose);

  [[nodiscard]] volatile char *get_starting_address() const;

  [[nodiscard]] uint64_t get_size() const { return size; }

  std::string get_flipped_rows_text_repr();
};

#endif //BLACKSMITH_SRC_MEMORY_H_
