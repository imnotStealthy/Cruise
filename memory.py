"""Read-only process memory access for FH6 (ctypes, no dependency).

STRICTLY read-only: the handle is opened with PROCESS_VM_READ |
PROCESS_QUERY_INFORMATION only — never PROCESS_VM_WRITE/OPERATION. Used to read
the credit balance to optimize the Garage Buyer (data collection, no game value
is ever modified). FH6 has no public offset, so:

  1. scan_u32() locates an exact value (your current CR) across readable regions;
  2. once you isolate the stable address, store a pointer chain (module base +
     offsets) in config; resolve_chain() reads it each run.

If the chain breaks after a game patch, the buyer falls back to manual input.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

_k32 = ctypes.windll.kernel32

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
# protections we can read from
_READABLE = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80  # RO/RW/WC/EXEC_R/EXEC_RW/EXEC_WC

_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD), ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD), ("__alignment2", wintypes.DWORD),
    ]


def pid_of(name: str) -> int | None:
    """First PID whose image matches `name` (via tasklist, no console window)."""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=4, creationflags=0x08000000,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def open_readonly(pid: int):
    """Open a READ-ONLY handle (no write access requested). None on failure."""
    h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    return h or None


def close(handle) -> None:
    if handle:
        _k32.CloseHandle(handle)


def module_base(pid: int, name: str) -> int | None:
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not _k32.Module32FirstW(snap, ctypes.byref(me)):
            return None
        while True:
            if me.szModule.lower() == name.lower():
                return ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value
            if not _k32.Module32NextW(snap, ctypes.byref(me)):
                return None
    finally:
        _k32.CloseHandle(snap)


def module_bounds(pid: int, name: str) -> tuple[int, int] | None:
    """(base, size) of the named module, or None."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not _k32.Module32FirstW(snap, ctypes.byref(me)):
            return None
        while True:
            if me.szModule.lower() == name.lower():
                base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value
                return base, me.modBaseSize
            if not _k32.Module32NextW(snap, ctypes.byref(me)):
                return None
    finally:
        _k32.CloseHandle(snap)


def read_bytes(handle, addr: int, size: int) -> bytes | None:
    buf = (ctypes.c_ubyte * size)()
    got = ctypes.c_size_t(0)
    ok = _k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
    if not ok or got.value != size:
        return None
    return bytes(buf)


def read_u32(handle, addr: int) -> int | None:
    b = read_bytes(handle, addr, 4)
    return struct.unpack("<I", b)[0] if b else None


def read_u64(handle, addr: int) -> int | None:
    b = read_bytes(handle, addr, 8)
    return struct.unpack("<Q", b)[0] if b else None


def resolve_chain(handle, base: int, offsets: list[int]) -> int | None:
    """Walk a pointer chain: addr = base; for each offset except the last,
    addr = *(addr + offset) (read as u64); final addr = addr + offsets[-1]."""
    if not offsets:
        return base
    addr = base + offsets[0]
    for off in offsets[1:]:
        ptr = read_u64(handle, addr)
        if ptr is None:
            return None
        addr = ptr + off
    return addr


def scan_u32(handle, value: int, *, max_hits: int = 200, writable_only: bool = True) -> list[int]:
    """Return addresses (4-byte aligned) holding `value` as little-endian u32.
    Defaults to writable regions only (the heap, where game state lives) — far
    faster than scanning read-only mapped files. Read-only access."""
    target = struct.pack("<I", value)
    hits: list[int] = []
    for region, rsize in _regions(handle, writable_only=writable_only):
        nxt = region + rsize
        pos = region
        while pos < nxt and len(hits) < max_hits:
            n = min(0x1000000, nxt - pos)  # 16 MB chunks: fewer RPM calls
            data = read_bytes(handle, pos, n)
            if data:
                start = 0
                while True:
                    i = data.find(target, start)
                    if i < 0:
                        break
                    if (pos + i) % 4 == 0:
                        hits.append(pos + i)
                        if len(hits) >= max_hits:
                            break
                    start = i + 1
            pos += n
        if len(hits) >= max_hits:
            break
    return hits


def _regions(handle, *, writable_only: bool, min_addr: int = 0x10000,
             max_addr: int = 0x7FFFFFFFFFFF):
    """Yield (base, size) of committed, readable (optionally writable) regions."""
    PAGE_RW = 0x04 | 0x40 | 0x08 | 0x80  # RW / EXEC_RW / WC / EXEC_WC
    addr = min_addr
    mbi = MEMORY_BASIC_INFORMATION64()
    size = ctypes.sizeof(mbi)
    while addr < max_addr:
        if _k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), size) != size:
            break
        base, rsize = mbi.BaseAddress, mbi.RegionSize
        nxt = base + rsize if rsize else addr + 0x1000
        ok = (mbi.State == MEM_COMMIT and (mbi.Protect & _READABLE)
              and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS)))
        if ok and (not writable_only or (mbi.Protect & PAGE_RW)):
            yield base, rsize
        addr = nxt if nxt > addr else addr + 0x1000


def build_pointer_index(handle, *, progress=None, max_ptrs: int = 60_000_000,
                        extra_ranges=()):
    """Scan writable regions (plus any extra_ranges, e.g. the module image) and
    collect every 8-byte-aligned value that looks like a heap pointer (in
    [0x10000, 0x7FFFFFFFFFFF]). Returns (values, locs) as parallel arrays sorted
    by value, for bisect lookups. Read-only."""
    import array
    values = array.array("Q")
    locs = array.array("Q")
    count = 0
    ranges = list(_regions(handle, writable_only=True)) + list(extra_ranges)
    for base, rsize in ranges:
        pos = base
        end = base + rsize
        while pos < end:
            n = min(0x100000, end - pos)
            data = read_bytes(handle, pos, n)
            if data:
                for i in range(0, len(data) - 7, 8):
                    v = int.from_bytes(data[i:i + 8], "little")
                    if 0x10000 <= v <= 0x7FFFFFFFFFFF:
                        values.append(v)
                        locs.append(pos + i)
                        count += 1
                        if count >= max_ptrs:
                            if progress:
                                progress(f"pointer cap {max_ptrs} reached")
                            pos = end
                            break
            pos += n
        if progress:
            progress(f"indexed {count} pointers (region 0x{base:x})")
    order = sorted(range(len(values)), key=lambda k: values[k])
    sv = array.array("Q", (values[k] for k in order))
    sl = array.array("Q", (locs[k] for k in order))
    return sv, sl


def _locs_pointing_to(index, target: int, max_offset: int):
    """Locations L where values[L] in [target - max_offset, target]; returns
    list of (location, offset) with offset = target - value."""
    import bisect
    values, locs = index
    lo = bisect.bisect_left(values, target - max_offset)
    hi = bisect.bisect_right(values, target)
    return [(locs[i], target - values[i]) for i in range(lo, hi)]


def pointer_scan(handle, target_addr: int, mod_base: int, mod_size: int, *,
                 max_offset: int = 0x1000, max_depth: int = 4, max_chains: int = 20,
                 progress=None) -> list[list[int]]:
    """Find static pointer chains [off0, off1, ...] such that, from mod_base,
    resolve_chain reaches target_addr. Read-only. Returns offset lists (the
    form stored in config as buyer_mem_offsets)."""
    if progress:
        progress("building pointer index (writable regions + module)...")
    index = build_pointer_index(handle, progress=progress,
                                extra_ranges=[(mod_base, mod_size)])
    mod_end = mod_base + mod_size
    chains: list[list[int]] = []
    # BFS over levels: each frontier item is (current_addr, offsets_built_so_far).
    # offsets are built from the FINAL offset backwards, so we prepend.
    frontier = [(target_addr, [])]
    for depth in range(max_depth):
        if progress:
            progress(f"level {depth + 1}: {len(frontier)} targets")
        nxt = []
        for addr, tail in frontier:
            for loc, off in _locs_pointing_to(index, addr, max_offset):
                chain = [off] + tail
                if mod_base <= loc < mod_end:  # static root reached
                    chains.append([loc - mod_base] + chain)
                    if len(chains) >= max_chains:
                        return chains
                else:
                    nxt.append((loc, chain))
        # keep the frontier bounded (closest pointers first)
        frontier = sorted(nxt, key=lambda t: t[1][0])[:2000]
        if not frontier:
            break
    return chains


def read_credits(cfg: dict) -> int | None:
    """Read the CR balance via the configured pointer chain, or None.
    cfg keys: buyer_mem_module, buyer_mem_offsets (hex strings or ints),
    buyer_mem_width (32|64)."""
    name = cfg.get("buyer_mem_module", cfg.get("game_process", "forzahorizon6.exe"))
    offsets = cfg.get("buyer_mem_offsets") or []
    if not offsets:
        return None
    offs = [int(o, 16) if isinstance(o, str) else int(o) for o in offsets]
    pid = pid_of(name)
    if pid is None:
        return None
    h = open_readonly(pid)
    if not h:
        return None
    try:
        base = module_base(pid, name)
        if base is None:
            return None
        addr = resolve_chain(h, base, offs)
        if addr is None:
            return None
        return read_u64(h, addr) if int(cfg.get("buyer_mem_width", 32)) == 64 else read_u32(h, addr)
    finally:
        close(h)
