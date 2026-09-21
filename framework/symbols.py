#!/usr/bin/env python3
"""Resolve symbols in shared libraries."""

import re
import subprocess
from typing import Optional, List, Tuple


def run_command(cmd: List[str]) -> str:
    """Run a command and return stdout."""
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=30
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return ""


def find_symbol_offset(so_path: str, symbol_patterns: List[str]) -> Optional[Tuple[int, str]]:
    """
    Find the offset of a symbol in a shared library.
    
    Args:
        so_path: Path to the .so file
        symbol_patterns: List of regex patterns to match (tried in order)
    
    Returns:
        (offset, matched_symbol_name) or None
    
    Uses:
        1. readelf -Ws (dynamic symbols) | c++filt
        2. nm -D (fallback)
    """
    # Method 1: readelf -Ws | c++filt (best for dynamic symbols)
    readelf_output = run_command(['readelf', '-Ws', so_path])
    if readelf_output:
        # Demangle C++ symbols
        try:
            demangled = subprocess.run(
                ['c++filt'],
                input=readelf_output,
                capture_output=True,
                text=True,
                timeout=10
            ).stdout
        except:
            demangled = readelf_output
        
        for pattern in symbol_patterns:
            result = search_readelf_output(demangled, pattern)
            if result:
                return result
    
    # Method 2: nm -D (fallback)
    nm_output = run_command(['nm', '-D', so_path])
    if nm_output:
        try:
            demangled = subprocess.run(
                ['c++filt'],
                input=nm_output,
                capture_output=True,
                text=True,
                timeout=10
            ).stdout
        except:
            demangled = nm_output
        
        for pattern in symbol_patterns:
            result = search_nm_output(demangled, pattern)
            if result:
                return result
    
    return None


def search_readelf_output(output: str, pattern: str) -> Optional[Tuple[int, str]]:
    """
    Search readelf -Ws output for a symbol matching the pattern.
    
    readelf format:
    Num:    Value          Size Type    Bind   Vis      Ndx Name
    123: 0000000000123456   100 FUNC    GLOBAL DEFAULT   12 symbol_name
    """
    regex = re.compile(pattern)
    
    for line in output.split('\n'):
        if regex.search(line):
            # Parse the line
            # Format: "   123: 0000000000123456   100 FUNC    GLOBAL DEFAULT   12 symbol_name"
            parts = line.split()
            if len(parts) >= 8:
                try:
                    # Value is the second field (index 1)
                    value_str = parts[1].rstrip(':')
                    if value_str.startswith('0'):
                        offset = int(value_str, 16)
                        # Symbol name is everything after the 7th field
                        symbol_name = ' '.join(parts[7:])
                        if offset > 0:  # Skip undefined symbols
                            return (offset, symbol_name)
                except (ValueError, IndexError):
                    pass
    
    return None


def search_nm_output(output: str, pattern: str) -> Optional[Tuple[int, str]]:
    """
    Search nm output for a symbol matching the pattern.
    
    nm format:
    0000000000123456 T symbol_name
    """
    regex = re.compile(pattern)
    
    for line in output.split('\n'):
        if regex.search(line):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    offset = int(parts[0], 16)
                    symbol_type = parts[1]
                    symbol_name = ' '.join(parts[2:])
                    # T = text (code), t = local text
                    if symbol_type in ('T', 't', 'W', 'w') and offset > 0:
                        return (offset, symbol_name)
                except (ValueError, IndexError):
                    pass
    
    return None


def list_symbols(so_path: str, filter_pattern: str = "") -> List[Tuple[int, str, str]]:
    """
    List all symbols in a shared library.
    
    Args:
        so_path: Path to the .so file
        filter_pattern: Optional regex to filter symbols
    
    Returns:
        List of (offset, type, name) tuples
    """
    symbols = []
    
    nm_output = run_command(['nm', '-D', so_path])
    if not nm_output:
        return symbols
    
    try:
        demangled = subprocess.run(
            ['c++filt'],
            input=nm_output,
            capture_output=True,
            text=True,
            timeout=10
        ).stdout
    except:
        demangled = nm_output
    
    regex = re.compile(filter_pattern) if filter_pattern else None
    
    for line in demangled.split('\n'):
        parts = line.split()
        if len(parts) >= 3:
            try:
                offset = int(parts[0], 16)
                sym_type = parts[1]
                name = ' '.join(parts[2:])
                
                if regex is None or regex.search(name):
                    symbols.append((offset, sym_type, name))
            except ValueError:
                pass
    
    return sorted(symbols)


# =============================================================================
# Specific symbol finders for our targets
# =============================================================================

def _find_call_instruction(so_path: str, caller_func_offset: int,
                           callee_func_offset: int) -> Optional[int]:
    """
    Find the 'call <callee>' instruction within the caller function by
    disassembling and scanning for the E8 (near call) opcode whose
    relative offset resolves to callee_func_offset.
    """
    try:
        output = run_command([
            'objdump', '-d',
            f'--start-address={hex(caller_func_offset)}',
            f'--stop-address={hex(caller_func_offset + 0x900)}',
            so_path
        ])
        if not output:
            return None
        
        # Look for "call <hex_addr> <callee>" where callee addr matches
        callee_hex = format(callee_func_offset, 'x')
        for line in output.split('\n'):
            if f'call' in line and callee_hex in line:
                parts = line.strip().split(':')
                if parts:
                    addr_str = parts[0].strip()
                    try:
                        return int(addr_str, 16)
                    except ValueError:
                        pass
    except Exception:
        pass
    return None


def find_openvino_set_tensor_symbol(so_path: str) -> Optional[Tuple[int, str]]:
    """
    Find the set_tensor function in OpenVINO runtime.
    
    Target: ov::InferRequest::set_tensor(string, Tensor)
    This is the version called when binding weights by name.
    
    """
    patterns = [
        # Exact match for set_tensor(string, Tensor) - not set_tensors
        r'InferRequest::set_tensor\(std::basic_string',
        r'InferRequest::set_tensor\(std::string',
        # Fallback patterns
        r'InferRequest.*set_tensor\(.*string',
    ]
    return find_symbol_offset(so_path, patterns)


def find_openvino_create_host_tensor_symbol(so_path: str) -> Optional[Tuple[int, str]]:
    """
    Find create_host_tensor in OpenVINO (for GPU context).
    
    This is called BEFORE data copy, making it a better BP2 target for rowhammer
    attacks that need to corrupt data before it's copied to the host tensor.
    
    Target: ov::RemoteContext::create_host_tensor
    """
    # Prefer concrete implementation first. IRemoteContext often appears too,
    # but may not be the executed call target in practice.
    candidates = list_symbols(so_path, r'create_host_tensor')
    exact_remote = []
    iremote = []
    other = []
    for off, sym_type, name in candidates:
        if "RemoteContext::create_host_tensor" in name and "IRemoteContext" not in name:
            exact_remote.append((off, name))
        elif "IRemoteContext::create_host_tensor" in name:
            iremote.append((off, name))
        elif "create_host_tensor" in name:
            other.append((off, name))

    if exact_remote:
        off, name = exact_remote[0]
        return (off, name)
    if iremote:
        off, name = iremote[0]
        return (off, name)
    if other:
        off, name = other[0]
        return (off, name)

    patterns = [
        r'ov::RemoteContext::create_host_tensor',
        r'[^A-Za-z0-9_]RemoteContext::create_host_tensor',
        r'create_host_tensor',
    ]
    return find_symbol_offset(so_path, patterns)


def find_plt_entry(so_path: str, symbol_pattern: str) -> Optional[Tuple[int, str]]:
    """
    Find a PLT (Procedure Linkage Table) entry for an imported function.
    
    PLT entries exist for functions imported from other .so files.
    They survive stripping because they're in .dynsym.
    
    Returns (plt_addr, mangled_or_demangled_name) or None.
    """
    output = run_command(['objdump', '-d', '-j', '.plt', so_path])
    if not output:
        return None
    
    try:
        demangled = subprocess.run(
            ['c++filt'], input=output,
            capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        demangled = output
    
    regex = re.compile(symbol_pattern)
    for line in demangled.split('\n'):
        if '@plt>' in line and regex.search(line):
            # Format: "0000000000063f80 <symbol@plt>:"
            parts = line.split()
            if parts:
                try:
                    addr = int(parts[0], 16)
                    name = line.split('<')[1].split('>')[0] if '<' in line else parts[0]
                    return (addr, name)
                except (ValueError, IndexError):
                    pass
    return None


def find_caller_side_call_sites(so_path: str, plt_addr: int,
                                symbol_desc: str = "") -> List[Tuple[int, str]]:
    """
    Find all CALL instructions in a .so that target a given PLT address.
    
    This discovers the "caller-side" instructions — the exact points in
    the code that invoke a specific imported function through the PLT.
    
    Args:
        so_path: Path to the .so file
        plt_addr: Address of the PLT entry (from find_plt_entry)
        symbol_desc: Human-readable description for logging
    
    Returns:
        List of (call_instr_addr, description) tuples
    """
    output = run_command(['objdump', '-d', so_path])
    if not output:
        return []
    
    plt_hex = format(plt_addr, 'x')
    results = []
    
    for line in output.split('\n'):
        if 'call' in line and plt_hex in line and '@plt>' in line:
            parts = line.strip().split(':')
            if parts:
                addr_str = parts[0].strip()
                try:
                    addr = int(addr_str, 16)
                    desc = f"call {symbol_desc or plt_hex}@plt"
                    results.append((addr, desc))
                except ValueError:
                    pass
    
    return results


def find_bp2_caller_side_candidates(pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Caller-side create_host_tensor sites in _pyopenvino.so."""
    # Step 1: Find PLT entry for create_host_tensor in _pyopenvino.so
    plt = find_plt_entry(pyopenvino_so_path, r'create_host_tensor')
    if not plt:
        return []
    
    plt_addr, plt_name = plt
    
    # Step 2: Find all CALL instructions targeting this PLT entry
    return find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "create_host_tensor")


def find_bp2_infer_caller_side_candidates(pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """
    Find caller-side CALL instructions for ov::InferRequest::infer()
    inside _pyopenvino.so.

    Returns list of (offset, description) for call-site candidates.
    """
    plt = find_plt_entry(pyopenvino_so_path, r'InferRequest.*\binfer\b')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'InferRequest5infer')
    if not plt:
        return []

    plt_addr, plt_name = plt

    return find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "InferRequest::infer")


def find_bp2_get_tensor_caller_side_candidates(pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Caller-side get_tensor sites in _pyopenvino.so."""
    plt = find_plt_entry(pyopenvino_so_path, r'\bget_tensor\(std::basic_string')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'\bget_tensor\(std::string')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'InferRequest.*get_tensor')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'\bget_tensor\(')
    if not plt:
        return []

    plt_addr, plt_name = plt

    return find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "InferRequest::get_tensor")


def find_openvino_compile_model_caller_side_candidates(pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Caller-side compile_model sites in _pyopenvino.so."""
    plt = find_plt_entry(pyopenvino_so_path, r'\bcompile_model\(')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'Core.*compile_model')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'compile_model')
    if not plt:
        return []

    plt_addr, plt_name = plt

    return find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "Core::compile_model")


def find_openvino_create_infer_request_caller_side_candidates(
        pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Caller-side create_infer_request sites in _pyopenvino.so."""
    plt = find_plt_entry(pyopenvino_so_path, r'\bcreate_infer_request\(')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'CompiledModel.*create_infer_request')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'create_infer_request')
    if not plt:
        return []

    plt_addr, plt_name = plt

    return find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "CompiledModel::create_infer_request")


def find_bp2_set_input_tensor_caller_side_candidates(
        pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Return sites of set_input_tensor in _pyopenvino.so."""
    plt = find_plt_entry(pyopenvino_so_path,
                         r'set_input_tensor\(unsigned')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path,
                             r'set_input_tensor\(ov::Tensor')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path, r'set_input_tensor')
    if not plt:
        return []

    plt_addr, plt_name = plt

    sites = find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "set_input_tensor")


    ret_sites = []
    for addr, desc in sites:
        ret_addr = addr + 5
        ret_desc = desc.replace("call ", "ret-from ")
        ret_sites.append((ret_addr, ret_desc))

    return ret_sites


def find_bp2_set_tensor_caller_side_candidates(
        pyopenvino_so_path: str) -> List[Tuple[int, str]]:
    """Return sites of set_tensor in _pyopenvino.so."""
    plt = find_plt_entry(pyopenvino_so_path,
                         r'\bset_tensor\(std::basic_string')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path,
                             r'\bset_tensor\(std::string')
    if not plt:
        plt = find_plt_entry(pyopenvino_so_path,
                             r'\bset_tensor\(')
    if not plt:
        return []

    plt_addr, plt_name = plt

    sites = find_caller_side_call_sites(
        pyopenvino_so_path, plt_addr, "set_tensor")


    ret_sites = []
    for addr, desc in sites:
        ret_addr = addr + 5
        ret_desc = desc.replace("call ", "ret-from ")
        ret_sites.append((ret_addr, ret_desc))

    return ret_sites


def find_memcpy_symbol(so_path: str) -> Optional[Tuple[int, str]]:
    """Find memcpy in libc."""
    patterns = [
        r'\bmemcpy\b',
        r'__memcpy',
    ]
    return find_symbol_offset(so_path, patterns)


def find_openvino_create_infer_request_symbol(so_path: str) -> Optional[Tuple[int, str]]:
    """
    Find create_infer_request in OpenVINO.
    """
    patterns = [
        r'create_infer_request',
        r'CompiledModel.*create.*request',
    ]
    return find_symbol_offset(so_path, patterns)


def find_level_zero_alloc_symbol(so_path: str) -> Optional[Tuple[int, str]]:
    """
    Find Level Zero memory allocation functions (for GPU shared memory).
    """
    patterns = [
        r'zeMemAllocShared',
        r'zeMemAllocDevice',
        r'zeMemAllocHost',
    ]
    return find_symbol_offset(so_path, patterns)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python symbols.py <so_path> [pattern]")
        sys.exit(1)

    so_path = sys.argv[1]
    pattern = sys.argv[2] if len(sys.argv) > 2 else ""
    
    if pattern:
        result = find_symbol_offset(so_path, [pattern])
        if result:
            offset, name = result
            print(f"Found: offset={hex(offset)}, symbol={name}")
        else:
            print(f"Symbol matching '{pattern}' not found")
    else:
        print(f"Symbols in {so_path}:")
        for offset, sym_type, name in list_symbols(so_path)[:50]:
            print(f"  {hex(offset):18s} {sym_type} {name}")
        print("...")
