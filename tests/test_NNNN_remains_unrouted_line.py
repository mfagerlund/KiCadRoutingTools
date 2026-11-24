#!/usr/bin/env python3
"""A second terminal failure of a stub-restored rip victim names the net.

The first terminal failure of a rip victim whose saved route is blocked
re-inserts the escape stub ('stub') and prints the RIP-RESTORE line for it.
The registry entry stays, so a second ladder failure of the same net reaches
try_terminal_restore again, finds the stub already on the board and returns
'stub' once more. Before this change it said nothing, so the log ended with
no line naming what became of the net (#1394: every unrouted net is
reported). The second call must now print that the net remains UNROUTED,
and still add no copper.

    python3 tests/test_NNNN_remains_unrouted_line.py
"""
import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'py_router'))
sys.path.insert(0, os.path.join(_ROOT, 'py_tools'))
sys.path.insert(0, os.path.join(_ROOT, 'tests'))

from routing_config import GridRouteConfig                     # noqa: E402
from rip_restore import try_terminal_restore                   # noqa: E402
from test_terminal_restore_idempotent import _board            # noqa: E402


def main():
    cfg = GridRouteConfig(clearance=0.2, track_width=0.2, via_size=0.6,
                          via_drill=0.3, grid_step=0.1,
                          layers=['F.Cu', 'B.Cu'])
    pcb, _seg, _via = _board()

    first = try_terminal_restore(pcb, cfg, 7)
    n_s1, n_v1 = len(pcb.segments), len(pcb.vias)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        second = try_terminal_restore(pcb, cfg, 7)
    text = out.getvalue()

    failures = []
    if first != 'stub':
        print(f"FAIL: fixture no longer reaches the 'stub' path "
              f"(verdict={first!r})")
        return 1
    if second != 'stub':
        failures.append(f"second call returned {second!r}, want 'stub'")
    if 'NET7 remains UNROUTED' not in text:
        failures.append(f"second call printed no UNROUTED line for NET7: "
                        f"{text!r}")
    if (len(pcb.segments), len(pcb.vias)) != (n_s1, n_v1):
        failures.append("second call added copper")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("test_NNNN_remains_unrouted_line: all checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
