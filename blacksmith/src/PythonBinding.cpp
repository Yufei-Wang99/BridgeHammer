/*
 * Python binding for Blacksmith rowhammer replay functionality.
 * This allows calling Blacksmith's replay functions from Python via ctypes,
 * running in the same process/address space.
 */

#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_set>
#include <vector>

#include "Blacksmith.hpp"
#include "GlobalDefines.hpp"
#include "Memory/Memory.hpp"

// Defined in Blacksmith.cpp for the CLI. The shared library needs its own copy.
ProgramArguments program_args;
#include "Memory/DRAMAddr.hpp"
#include "Memory/DramAnalyzer.hpp"
#include "Forges/ReplayingHammerer.hpp"
#include "Forges/FuzzyHammerer.hpp"
#include "Utilities/Logger.hpp"

// Global state for the binding
static Memory* g_memory = nullptr;
static ReplayingHammerer* g_replayer = nullptr;
static DramAnalyzer* g_dram_analyzer = nullptr;
static bool g_initialized = false;

extern "C" {

/**
 * Initialize the Blacksmith library.
 * Must be called before any other functions.
 * 
 * @param num_ranks Number of ranks on the DIMM (1 or 2)
 * @param mem_size_mb Memory size to allocate in MB (default: 256)
 * @param use_superpage Ignored; allocation is always 4KB pages.
 * @return 0 on success, -1 on failure
 */
int bs_init(int num_ranks, int mem_size_mb, int use_superpage) {
    (void) use_superpage;
    if (g_initialized) {
        Logger::log_info("Blacksmith already initialized");
        return 0;
    }

    try {
        Logger::initialize();
        Logger::log_info("Initializing Blacksmith Python binding...");

        int mem_mb = mem_size_mb > 0 ? mem_size_mb : 4;
        size_t mem_size = static_cast<size_t>(mem_mb) * 1024 * 1024;
        g_memory = new Memory(false);
        g_memory->allocate_memory(mem_size);

        // Held-VA replay does not need a spray catalog or bank-conflict hunt.
        // num_ranks still selects the DRAM matrix (4 functions = 1 rank).
        if (num_ranks <= 0) {
            Logger::log_error("Invalid num_ranks, must be > 0");
            return -1;
        }
        g_dram_analyzer = new DramAnalyzer(g_memory->get_starting_address(), mem_size);
        g_dram_analyzer->load_known_functions(num_ranks);
        DRAMAddr::initialize(g_dram_analyzer->get_bank_rank_functions().size(),
                            g_memory->get_starting_address());

        // Create replayer
        g_replayer = new ReplayingHammerer(*g_memory);

        g_initialized = true;
        Logger::log_success("Blacksmith initialized successfully");
        return 0;
    } catch (const std::exception& e) {
        Logger::log_error(format_string("Initialization failed: %s", e.what()));
        return -1;
    }
}

/**
 * Cleanup and free resources.
 */
void bs_cleanup() {
    if (g_replayer) {
        delete g_replayer;
        g_replayer = nullptr;
    }
    if (g_dram_analyzer) {
        delete g_dram_analyzer;
        g_dram_analyzer = nullptr;
    }
    if (g_memory) {
        delete g_memory;
        g_memory = nullptr;
    }
    g_initialized = false;
    Logger::log_info("Blacksmith cleanup complete");
    Logger::close();
}

/**
 * Check if library is initialized.
 */
int bs_is_initialized() {
    return g_initialized ? 1 : 0;
}

/**
 * Replay patterns from a JSON file.
 * 
 * @param json_path Path to the fuzz-summary.json file
 * @param pattern_ids Comma-separated pattern IDs to replay (NULL for all)
 * @param sweep_mb Memory size to sweep in MB (0 to disable sweeping)
 * @return Number of bit flips found, or -1 on error
 */
int bs_replay_patterns(const char* json_path, const char* pattern_ids, int sweep_mb) {
    if (!g_initialized || !g_replayer) {
        Logger::log_error("Blacksmith not initialized. Call bs_init() first.");
        return -1;
    }

    try {
        std::unordered_set<std::string> ids;
        if (pattern_ids != nullptr && strlen(pattern_ids) > 0) {
            // Parse comma-separated pattern IDs
            std::string ids_str(pattern_ids);
            size_t pos = 0;
            while ((pos = ids_str.find(',')) != std::string::npos) {
                std::string id = ids_str.substr(0, pos);
                // Trim whitespace
                id.erase(0, id.find_first_not_of(" \t"));
                id.erase(id.find_last_not_of(" \t") + 1);
                if (!id.empty()) ids.insert(id);
                ids_str.erase(0, pos + 1);
            }
            // Last token
            ids_str.erase(0, ids_str.find_first_not_of(" \t"));
            ids_str.erase(ids_str.find_last_not_of(" \t") + 1);
            if (!ids_str.empty()) ids.insert(ids_str);
        }

        Logger::log_info(format_string("Replaying patterns from: %s", json_path));
        if (!ids.empty()) {
            Logger::log_info(format_string("Pattern IDs: %zu specified", ids.size()));
        }

        size_t sweep_bytes = static_cast<size_t>(sweep_mb) * 1024 * 1024;
        
        if (sweep_bytes > 0) {
            // Use sweep mode
            size_t bitflips = g_replayer->replay_patterns_brief(
                std::string(json_path), ids, sweep_bytes, false);
            return static_cast<int>(bitflips);
        } else {
            // Regular replay
            g_replayer->replay_patterns(std::string(json_path), ids);
            return 0;  // replay_patterns doesn't return count
        }
    } catch (const std::exception& e) {
        Logger::log_error(format_string("Replay failed: %s", e.what()));
        return -1;
    }
}

/**
 * Replay the loaded DIMM pattern once per mapped PFN.
 * num_reps is ReplayingHammerer::hammer_pattern's outer loop (not * 1e6).
 */
int bs_replay_pages(const char* json_path, const uint64_t* pfns, int n_pfns, int num_reps) {
    if (!g_initialized || !g_replayer) {
        Logger::log_error("Blacksmith not initialized. Call bs_init() first.");
        return -1;
    }
    if (json_path == nullptr || pfns == nullptr || n_pfns <= 0) {
        Logger::log_error("bs_replay_pages: invalid arguments");
        return -1;
    }
    try {
        std::vector<uint64_t> pages(pfns, pfns + n_pfns);
        unsigned long reps = num_reps > 0 ? static_cast<unsigned long>(num_reps) : 500UL;
        Logger::log_info(format_string(
            "bs_replay_pages json=%s pages=%d reps=%lu", json_path, n_pfns, reps));
        return static_cast<int>(g_replayer->replay_pages(std::string(json_path), pages, reps));
    } catch (const std::exception& e) {
        Logger::log_error(format_string("bs_replay_pages failed: %s", e.what()));
        return -1;
    }
}

/**
 * Hammer the given virtual addresses with the loaded pattern.
 * addrs is the access sequence (neighbor/aggressor VAs the attacker still holds).
 */
int bs_replay_access_vas(const char* json_path, const uint64_t* addrs, int n_addrs, int num_reps) {
    if (!g_initialized || !g_replayer) {
        Logger::log_error("Blacksmith not initialized. Call bs_init() first.");
        return -1;
    }
    if (json_path == nullptr || addrs == nullptr || n_addrs <= 0) {
        Logger::log_error("bs_replay_access_vas: invalid arguments");
        return -1;
    }
    try {
        std::vector<volatile char *> accesses;
        accesses.reserve(static_cast<size_t>(n_addrs));
        for (int i = 0; i < n_addrs; i++) {
            accesses.push_back(reinterpret_cast<volatile char *>(addrs[i]));
        }
        unsigned long reps = num_reps > 0 ? static_cast<unsigned long>(num_reps) : 500UL;
        return static_cast<int>(
            g_replayer->replay_access_vas(std::string(json_path), accesses, reps));
    } catch (const std::exception& e) {
        Logger::log_error(format_string("bs_replay_access_vas failed: %s", e.what()));
        return -1;
    }
}

/**
 * Get the virtual address of Blacksmith's allocated memory region.
 * This can be used to understand where Blacksmith's memory is mapped.
 */
uint64_t bs_get_memory_base() {
    if (!g_initialized || !g_memory) {
        return 0;
    }
    return reinterpret_cast<uint64_t>(g_memory->get_starting_address());
}

/**
 * Hammer a specific pair of addresses (double-sided rowhammer).
 * The addresses should be in the same bank but different rows.
 * 
 * @param addr1 First aggressor address
 * @param addr2 Second aggressor address  
 * @param iterations Number of hammer iterations (millions)
 */
void bs_hammer_double_sided(uint64_t addr1, uint64_t addr2, int iterations) {
    volatile char* a1 = reinterpret_cast<volatile char*>(addr1);
    volatile char* a2 = reinterpret_cast<volatile char*>(addr2);
    
    int total_iters = iterations * 1000000;
    
    for (int i = 0; i < total_iters; i++) {
        // Access both addresses
        *a1;
        *a2;
        
        // Flush from cache
        asm volatile("clflushopt (%0)\n" :: "r"(a1) : "memory");
        asm volatile("clflushopt (%0)\n" :: "r"(a2) : "memory");
        asm volatile("mfence" ::: "memory");
    }
}

/**
 * Hammer addresses from an array (n-sided rowhammer).
 * 
 * @param addrs Array of addresses to hammer
 * @param num_addrs Number of addresses in the array
 * @param iterations Number of hammer iterations (millions)
 */
void bs_hammer_n_sided(uint64_t* addrs, int num_addrs, int iterations) {
    if (num_addrs <= 0 || addrs == nullptr) return;
    
    std::vector<volatile char*> addr_vec;
    for (int i = 0; i < num_addrs; i++) {
        addr_vec.push_back(reinterpret_cast<volatile char*>(addrs[i]));
    }
    
    int total_iters = iterations * 1000000;
    
    for (int i = 0; i < total_iters; i++) {
        // Access all addresses
        for (auto addr : addr_vec) {
            *addr;
        }
        
        // Flush all from cache
        for (auto addr : addr_vec) {
            asm volatile("clflushopt (%0)\n" :: "r"(addr) : "memory");
        }
        asm volatile("mfence" ::: "memory");
    }
}

/**
 * Flush a cache line.
 */
void bs_clflush(uint64_t addr) {
    asm volatile("clflushopt (%0)\n" :: "r"(addr) : "memory");
}

/**
 * Memory fence.
 */
void bs_mfence() {
    asm volatile("mfence" ::: "memory");
}

/**
 * Convert a virtual address to DRAM address components.
 * 
 * @param vaddr Virtual address
 * @param bank Output: bank number
 * @param row Output: row number
 * @param col Output: column number
 * @return 0 on success, -1 if not initialized
 */
int bs_vaddr_to_dram(uint64_t vaddr, int* bank, int* row, int* col) {
    if (!g_initialized) {
        return -1;
    }
    
    DRAMAddr daddr(reinterpret_cast<void*>(vaddr));
    *bank = static_cast<int>(daddr.bank);
    *row = static_cast<int>(daddr.row);
    *col = static_cast<int>(daddr.col);
    return 0;
}

/**
 * Get the number of activations per tREFI (refresh interval).
 * This is useful for calibrating hammer parameters.
 */
int bs_get_acts_per_trefi() {
    if (!g_initialized || !g_dram_analyzer) {
        return -1;
    }
    return static_cast<int>(g_dram_analyzer->count_acts_per_trefi());
}

} // extern "C"


