"""Locate the FH6 credit value in memory (READ-ONLY).

Run repeatedly, narrowing candidates between runs by gaining/spending CR:

  py -3 tools/scan_credits.py 84489119
  # spend or earn some CR in-game, note the new balance, then:
  py -3 tools/scan_credits.py 84142369 --prev addrs.txt

Each run writes the matching addresses to addrs.txt. When --prev is given, only
addresses that matched last time AND hold the new value survive -> the list
collapses to a handful, usually one stable address.

This finds a raw ADDRESS, which moves each launch. To get a permanent pointer
chain (module base + offsets) you still need a pointer scan (Cheat Engine).
This script is only the value-scan half, with no write access whatsoever.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402

PROC = "ForzaHorizon6.exe"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: scan_credits.py <current_CR_value> [--prev addrs.txt]")
        return
    value = int(args[0].replace(",", "").replace("_", ""))
    prev_path = None
    if "--prev" in sys.argv:
        prev_path = sys.argv[sys.argv.index("--prev") + 1]

    pid = memory.pid_of(PROC)
    if pid is None:
        print(f"{PROC} not running.")
        return
    h = memory.open_readonly(pid)
    if not h:
        print("OpenProcess failed (run this terminal as Administrator).")
        return
    try:
        if prev_path and Path(prev_path).exists():
            prev = [int(x, 16) for x in Path(prev_path).read_text().split()]
            hits = [a for a in prev if memory.read_u32(h, a) == value]
            print(f"narrowed {len(prev)} -> {len(hits)} addresses still == {value}")
        else:
            hits = memory.scan_u32(h, value)
            print(f"found {len(hits)} addresses == {value}")
    finally:
        memory.close(h)

    Path("addrs.txt").write_text("\n".join(f"{a:x}" for a in hits))
    for a in hits[:20]:
        print(f"  0x{a:x}")
    if len(hits) > 20:
        print(f"  ... +{len(hits) - 20} more (full list in addrs.txt)")
    print("Re-run after changing your CR with: --prev addrs.txt")


if __name__ == "__main__":
    main()
