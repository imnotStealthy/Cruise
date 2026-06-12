"""Find a STATIC pointer chain to the FH6 credit value (READ-ONLY).

Step 1 — find the live address (run as Administrator, FH6 open):
    py -3 tools/scan_credits.py 84489119
    # change CR in-game, then narrow:
    py -3 tools/scan_credits.py <new_value> --prev addrs.txt
    # repeat until ONE address remains in addrs.txt

Step 2 — turn that address into a permanent chain:
    py -3 tools/pointer_scan.py 0x<address_from_addrs.txt>

It prints chains [off0, off1, ...] rooted at ForzaHorizon6.exe+off0. Pick one
and put it in config.json:  "buyer_mem_offsets": ["0x...", "0x...", ...]
Verify it survives a game restart (re-run scan_credits, re-run this) before
trusting it. Strictly read-only: no memory is ever written.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402

PROC = "ForzaHorizon6.exe"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: pointer_scan.py 0x<target_address> [--depth N] [--off 0x1000]")
        return
    target = int(args[0], 16)
    depth = int(sys.argv[sys.argv.index("--depth") + 1]) if "--depth" in sys.argv else 4
    max_off = int(sys.argv[sys.argv.index("--off") + 1], 16) if "--off" in sys.argv else 0x1000

    pid = memory.pid_of(PROC)
    if pid is None:
        print(f"{PROC} not running.")
        return
    bounds = memory.module_bounds(pid, PROC)
    if not bounds:
        print("module base not found (run as Administrator).")
        return
    base, size = bounds
    h = memory.open_readonly(pid)
    if not h:
        print("OpenProcess failed (run as Administrator).")
        return
    try:
        chains = memory.pointer_scan(h, target, base, size, max_offset=max_off,
                                     max_depth=depth, progress=lambda m: print(" ", m))
    finally:
        memory.close(h)

    if not chains:
        print("No static chain found. Try --depth 5 or --off 0x2000.")
        return
    print(f"\n{len(chains)} chain(s) — shortest first:")
    for c in sorted(chains, key=len)[:20]:
        offs = ", ".join(f'"0x{o:x}"' for o in c)
        print(f'  buyer_mem_offsets: [{offs}]')
    # verify the shortest actually reads the target
    best = min(chains, key=len)
    h2 = memory.open_readonly(pid)
    try:
        addr = memory.resolve_chain(h2, base, best)
        print(f"\nverify shortest -> resolves to 0x{addr:x} (target 0x{target:x}) "
              f"{'OK' if addr == target else 'MISMATCH'}")
    finally:
        memory.close(h2)


if __name__ == "__main__":
    main()
