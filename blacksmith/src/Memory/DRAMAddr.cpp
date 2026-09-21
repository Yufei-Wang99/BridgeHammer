#include "Memory/DRAMAddr.hpp"
#include "GlobalDefines.hpp"
#include <fcntl.h>
#include <unistd.h>
#include <iostream>
#include <cstring>
#include <unordered_map>

// initialize static variable
std::map<size_t, MemConfiguration> DRAMAddr::Configs;
std::unordered_map<uint64_t, volatile char *> DRAMAddr::row_to_va;
std::unordered_map<uint64_t, DRAMAddr> DRAMAddr::va_to_dram;

void DRAMAddr::initialize(uint64_t num_bank_rank_functions, volatile char *start_address) {
  // TODO: This is a shortcut to check if it's a single rank dimm or dual rank in order to load the right memory
  //  configuration. We should get these infos from dmidecode to do it properly, but for now this is easier.
  size_t num_ranks;
  if (num_bank_rank_functions==5) {
    num_ranks = RANKS(2);
  } else if (num_bank_rank_functions==4) {
    num_ranks = RANKS(1);
  } else {
    Logger::log_error("Could not initialize DRAMAddr as #ranks seems not to be 1 or 2.");
    exit(1);
  }
  DRAMAddr::load_mem_config((CHANS(CHANNEL) | DIMMS(DIMM) | num_ranks | BANKS(NUM_BANKS)));
  DRAMAddr::set_base_msb((void *) start_address);
}

void DRAMAddr::set_base_msb(void *buff) {
  base_msb = (size_t) buff & (~((size_t) (1ULL << 30UL) - 1UL));  // get higher order bits above the super page
}

// TODO we can create a DRAMconfig class to load the right matrix depending on
// the configuration. You could also test it by checking if you can trigger bank conflcits
void DRAMAddr::load_mem_config(mem_config_t cfg) {
  DRAMAddr::initialize_configs();
  MemConfig = Configs[cfg];
}

DRAMAddr::DRAMAddr() = default;

DRAMAddr::DRAMAddr(size_t bk, size_t r, size_t c) {
  bank = bk;
  row = r;
  col = c;
}

uint64_t DRAMAddr::row_key(size_t bank, size_t row) {
  return (static_cast<uint64_t>(bank) << 32U) | static_cast<uint64_t>(row);
}

void DRAMAddr::decode_matrix(size_t p, size_t *bank, size_t *row, size_t *col) {
  size_t res = 0;
  for (unsigned long i : MemConfig.DRAM_MTX) {
    res <<= 1ULL;
    res |= (size_t) __builtin_parityl(p & i);
  }
  *bank = (res >> MemConfig.BK_SHIFT) & MemConfig.BK_MASK;
  *row = (res >> MemConfig.ROW_SHIFT) & MemConfig.ROW_MASK;
  *col = (res >> MemConfig.COL_SHIFT) & MemConfig.COL_MASK;
}

size_t DRAMAddr::linearize() const {
  return (this->bank << MemConfig.BK_SHIFT) | (this->row << MemConfig.ROW_SHIFT) | (this->col << MemConfig.COL_SHIFT);
}

DRAMAddr::DRAMAddr(void *addr) {
  const uint64_t page = (uint64_t) addr & ~0xFFFULL;
  auto it = va_to_dram.find(page);
  if (it != va_to_dram.end()) {
    bank = it->second.bank;
    row = it->second.row;
    col = it->second.col;
    return;
  }
  decode_matrix((size_t) addr, &bank, &row, &col);
}

DRAMAddr DRAMAddr::from_phys(uint64_t phys) {
  DRAMAddr out;
  decode_matrix((size_t) phys, &out.bank, &out.row, &out.col);
  return out;
}

void DRAMAddr::clear_page_map() {
  row_to_va.clear();
  va_to_dram.clear();
}

void DRAMAddr::register_page(void *va, uint64_t phys) {
  if (va == nullptr || phys == 0) return;
  DRAMAddr decoded = from_phys(phys);
  const uint64_t page = (uint64_t) va & ~0xFFFULL;
  va_to_dram[page] = decoded;
  const uint64_t key = row_key(decoded.bank, decoded.row);
  if (row_to_va.find(key) == row_to_va.end()) {
    row_to_va[key] = reinterpret_cast<volatile char *>(page);
  }
}

size_t DRAMAddr::catalog_range(volatile char *start, size_t nbytes) {
  if (start == nullptr || nbytes == 0) return 0;
  int fd = open("/proc/self/pagemap", O_RDONLY);
  if (fd < 0) {
    Logger::log_error("Cannot open /proc/self/pagemap to catalog 4KB pages.");
    return 0;
  }
  const size_t page = 4096;
  size_t mapped = 0;
  for (size_t off = 0; off < nbytes; off += page) {
    const uint64_t va = (uint64_t) (start + off);
    uint64_t entry = 0;
    const off_t poff = static_cast<off_t>((va / page) * sizeof(uint64_t));
    if (pread(fd, &entry, sizeof(entry), poff) != static_cast<ssize_t>(sizeof(entry))) {
      continue;
    }
    if ((entry & (1ULL << 63)) == 0) {
      continue;
    }
    const uint64_t pfn = entry & ((1ULL << 55) - 1);
    if (pfn == 0) {
      continue;
    }
    register_page(reinterpret_cast<void *>(va), pfn << 12);
    mapped++;
  }
  close(fd);
  return mapped;
}

bool DRAMAddr::has_row(size_t bank, size_t row) {
  return row_to_va.find(row_key(bank, row)) != row_to_va.end();
}

size_t DRAMAddr::mapped_row_count() {
  return row_to_va.size();
}

void *DRAMAddr::to_virt() {
  return const_cast<const DRAMAddr *>(this)->to_virt();
}

void *DRAMAddr::to_virt() const {
  auto it = row_to_va.find(row_key(bank, row));
  if (it != row_to_va.end()) {
    return (void *) it->second;
  }
  // After a 4KB catalog is built, missing rows must not fall back to the
  // hugepage identity mapping (VA lower bits != PA).
  if (!row_to_va.empty()) {
    return nullptr;
  }
  size_t res = 0;
  size_t l = this->linearize();
  for (unsigned long i : MemConfig.ADDR_MTX) {
    res <<= 1ULL;
    res |= (size_t) __builtin_parityl(l & i);
  }
  return (void *) (base_msb | res);
}

uint64_t DRAMAddr::to_phys() const {
  void *v_addr = this->to_virt();
  int pagemap = open("/proc/self/pagemap", O_RDONLY);
  if (pagemap < 0) {
    std::cerr << "Error: Cannot open /proc/self/pagemap" << std::endl;
    return 0;
  }
  uint64_t offset = ((uint64_t)v_addr / 4096) * sizeof(uint64_t);
  uint64_t entry;
  pread(pagemap, &entry, sizeof(entry), offset);
  close(pagemap);
  if (!(entry & (1ULL << 63))) {
    return 0;
  }
  return (entry & ((1ULL << 54) - 1)) * 4096;
}

std::string DRAMAddr::to_string() {
  char buff[1024];
  sprintf(buff, "DRAMAddr(b: %zu, r: %zu, c: %zu) = %p",
      this->bank,
      this->row,
      this->col,
      this->to_virt());
  return std::string(buff);
}

std::string DRAMAddr::to_string_compact() const {
  char buff[1024];
  sprintf(buff, "(%ld,%ld,%ld)",
      this->bank,
      this->row,
      this->col);
  return std::string(buff);
}

DRAMAddr DRAMAddr::add(size_t bank_increment, size_t row_increment, size_t column_increment) const {
  return {bank + bank_increment, row + row_increment, col + column_increment};
}

void DRAMAddr::add_inplace(size_t bank_increment, size_t row_increment, size_t column_increment) {
  bank += bank_increment;
  row += row_increment;
  col += column_increment;
}

// Define the static DRAM configs
MemConfiguration DRAMAddr::MemConfig;
size_t DRAMAddr::base_msb;

#ifdef ENABLE_JSON

nlohmann::json DRAMAddr::get_memcfg_json() {
  std::map<size_t, nlohmann::json> memcfg_to_json = {
      {(CHANS(1UL) | DIMMS(1UL) | RANKS(1UL) | BANKS(8UL)),
       nlohmann::json{
           {"channels", 1},
           {"dimms", 1},
           {"ranks", 1},
           {"banks", 8}}},
  };
  return memcfg_to_json[MemConfig.IDENTIFIER];
}

#endif

void DRAMAddr::initialize_configs() {
  struct MemConfiguration dram_cfg = {
      .IDENTIFIER = (CHANS(1UL) | DIMMS(1UL) | RANKS(1UL) | BANKS(8UL)),
      .BK_SHIFT = 26,
      .BK_MASK = (0b000000000000000000000000001111),
      .ROW_SHIFT = 0,
      .ROW_MASK = (0b000000000000000001111111111111),
      .COL_SHIFT = 13,
      .COL_MASK = (0b000000000000000001111111111111),
      .DRAM_MTX = {          
          0b000000000000000010000001000000,
          0b000000000000100100000000000000,
          0b000000000001001000000000000000,
          0b000000000010010000000000000000,
          0b000000000000000001000000000000,
          0b000000000000000000100000000000,
          0b000000000000000000010000000000,
          0b000000000000000000001000000000,
          0b000000000000000000000100000000,
          0b000000000000000000000010000000,
          0b000000000000000000000001000000,
          0b000000000000000000000000100000,
          0b000000000000000000000000010000,
          0b000000000000000000000000001000,
          0b000000000000000000000000000100,
          0b000000000000000000000000000010,
          0b000000000000000000000000000001,
          0b100000000000000000000000000000,
          0b010000000000000000000000000000,
          0b001000000000000000000000000000,
          0b000100000000000000000000000000,
          0b000010000000000000000000000000,
          0b000001000000000000000000000000,
          0b000000100000000000000000000000,
          0b000000010000000000000000000000,
          0b000000001000000000000000000000,
          0b000000000100000000000000000000,
          0b000000000010000000000000000000,
          0b000000000001000000000000000000,
          0b000000000000100000000000000000
       },
      .ADDR_MTX = {          
          0b000000000000000001000000000000,
          0b000000000000000000100000000000,
          0b000000000000000000010000000000,
          0b000000000000000000001000000000,
          0b000000000000000000000100000000,
          0b000000000000000000000010000000,
          0b000000000000000000000001000000,
          0b000000000000000000000000100000,
          0b000000000000000000000000010000,
          0b000000000000000000000000001000,
          0b000000000000000000000000000100,
          0b000000000000000000000000000010,
          0b000000000000000000000000000001,
          0b000100000000000000000000000100,
          0b001000000000000000000000000010,
          0b010000000000000000000000000001,
          0b100000000010000000000000000000,
          0b000010000000000000000000000000,
          0b000001000000000000000000000000,
          0b000000100000000000000000000000,
          0b000000010000000000000000000000,
          0b000000001000000000000000000000,
          0b000000000100000000000000000000,
          0b000000000010000000000000000000,
          0b000000000001000000000000000000,
          0b000000000000100000000000000000,
          0b000000000000010000000000000000,
          0b000000000000001000000000000000,
          0b000000000000000100000000000000,
          0b000000000000000010000000000000
       }
  };
  DRAMAddr::Configs = {
       {(CHANS(1UL) | DIMMS(1UL) | RANKS(1UL) | BANKS(8UL)), dram_cfg}
 };
}

#ifdef ENABLE_JSON

void to_json(nlohmann::json &j, const DRAMAddr &p) {
  j = {{"bank", p.bank},
       {"row", p.row},
       {"col", p.col}
  };
}

void from_json(const nlohmann::json &j, DRAMAddr &p) {
  j.at("bank").get_to(p.bank);
  j.at("row").get_to(p.row);
  j.at("col").get_to(p.col);
}

#endif
