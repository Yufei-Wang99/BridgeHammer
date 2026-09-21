/*
 * Copyright (c) 2021 by ETH Zurich.
 * Licensed under the MIT License, see LICENSE file for more details.
 */

#ifndef DRAMADDR
#define DRAMADDR

#include <cstdint>
#include <map>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#ifdef ENABLE_JSON
#include <nlohmann/json.hpp>
#endif

#define CHANS(x) ((x) << (8UL * 3UL))
#define DIMMS(x) ((x) << (8UL * 2UL))
#define RANKS(x) ((x) << (8UL * 1UL))
#define BANKS(x) ((x) << (8UL * 0UL))

#define MTX_SIZE (30)

typedef size_t mem_config_t;

struct MemConfiguration {
  size_t IDENTIFIER;
  size_t BK_SHIFT;
  size_t BK_MASK;
  size_t ROW_SHIFT;
  size_t ROW_MASK;
  size_t COL_SHIFT;
  size_t COL_MASK;
  size_t DRAM_MTX[MTX_SIZE];
  size_t ADDR_MTX[MTX_SIZE];
};

class DRAMAddr {
 private:
  // Class attributes
  static std::map<size_t, MemConfiguration> Configs;
  static MemConfiguration MemConfig;
  static size_t base_msb;
  static std::unordered_map<uint64_t, volatile char *> row_to_va;
  static std::unordered_map<uint64_t, DRAMAddr> va_to_dram;

  [[nodiscard]] size_t linearize() const;
  static uint64_t row_key(size_t bank, size_t row);
  static void decode_matrix(size_t p, size_t *bank, size_t *row, size_t *col);

 public:
  size_t bank{};
  size_t row{};
  size_t col{};

  // class methods
  static void set_base_msb(void *buff);

  static void load_mem_config(mem_config_t cfg);

  // instance methods
  DRAMAddr(size_t bk, size_t r, size_t c);

  explicit DRAMAddr(void *addr);

  // Decode a physical address with DRAM_MTX (pfn << 12 for a 4KB page).
  static DRAMAddr from_phys(uint64_t phys);

  static void clear_page_map();
  static void register_page(void *va, uint64_t phys);
  static size_t catalog_range(volatile char *start, size_t nbytes);
  static bool has_row(size_t bank, size_t row);
  static size_t mapped_row_count();

  // must be DefaultConstructible for JSON (de-)serialization
  DRAMAddr();

  void *to_virt();
  
  uint64_t to_phys() const;

  [[gnu::unused]] std::string to_string();

  static void initialize(uint64_t num_bank_rank_functions, volatile char *start_address);

  [[nodiscard]] std::string to_string_compact() const;

  [[nodiscard]] void *to_virt() const;

  [[nodiscard]] DRAMAddr add(size_t bank_increment, size_t row_increment, size_t column_increment) const;

  void add_inplace(size_t bank_increment, size_t row_increment, size_t column_increment);

#ifdef ENABLE_JSON
  static nlohmann::json get_memcfg_json();
#endif

  static void initialize_configs();
};

#ifdef ENABLE_JSON

void to_json(nlohmann::json &j, const DRAMAddr &p);

void from_json(const nlohmann::json &j, DRAMAddr &p);

#endif

#endif /* DRAMADDR */
