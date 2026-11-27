#!/usr/bin/env python3
"""A clearance rung below ring + hole clearance must still hold the board's
copper-to-hole clearance (KICAD_HOLE_FLOOR).

A rescue rung routes at a copper clearance under the run's (0.127 against
0.1548 here). A 0.7/0.4 via has a 0.15 ring, so copper kept 0.127 off its
ring sits 0.277 off its hole: under a board min_hole_clearance of 0.3048,
which KiCad reports as hole_clearance. With no .kicad_dru hole rule the
keep-outs used to grow by nothing.

  1. knob off: no growth (the old behaviour);
  2. knob on: a new via's keep-out off a track, a pad or another 0.7/0.4
     via grows by 0.2 + 0.3048 - 0.35 - 0.127 = 0.0278;
  3. knob on, at the run clearance 0.1548 the ring already holds: no growth;
  4. knob on: a track keeps 0.2 + 0.3048 + w/2 off an existing via's centre,
     on every layer, and more cells are blocked than with the knob off;
  5. no hole clearance installed: no growth with the knob on.

    python3 tests/test_1990_board_hole_floor_rungs.py
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import env_knobs  # noqa: E402
import fab_tiers  # noqa: E402
from obstacle_map import _via_track_expansion_per_layer  # noqa: E402
from routing_config import GridRouteConfig, GridCoord  # noqa: E402
from routing_utils import via_track_cells  # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ''))
    if not cond:
        FAILS.append(name)


def cfg(clearance=0.127):
    return GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.05, clearance=clearance,
                           track_width=0.127, via_size=0.7, via_drill=0.4)


def main():
    via = SimpleNamespace(x=10.0, y=10.0, size=0.7, drill=0.4, net_id=3,
                          layers=['F.Cu', 'B.Cu'])
    pad = SimpleNamespace(net_id=4, layers=['F.Cu'])
    want = 0.2 + 0.3048 - 0.35 - 0.127
    old = getattr(env_knobs, 'HOLE_FLOOR', False)
    fab_tiers.set_via_hole_clearance(0.3048, 0.1548)
    try:
        c = cfg()
        coord = GridCoord(c.grid_step)
        env_knobs.HOLE_FLOOR = False
        check("knob off: no growth", c.via_hole_extra(0.127) == 0.0)
        off = _via_track_expansion_per_layer(0.7, c, coord, 0.127, via=via)

        env_knobs.HOLE_FLOOR = True
        check("knob on: via off a track", abs(c.via_hole_extra(0.127) - want) < 1e-9,
              f"{c.via_hole_extra(0.127):.4f}")
        check("knob on: via off a pad", abs(c.via_hole_extra(0.127, pad=pad) - want) < 1e-9)
        check("knob on: via off another via", abs(c.via_hole_extra(0.127, via) - want) < 1e-9)
        check("knob on: ring holds at 0.1548", c.via_hole_extra(0.1548) < 1e-9,
              f"{c.via_hole_extra(0.1548):.4f}")
        on = _via_track_expansion_per_layer(0.7, c, coord, 0.127, via=via)
        reach = 0.2 + 0.3048 + c.route_reserve_width('F.Cu') / 2
        check("knob on: track reach off the via's hole",
              all(o >= via_track_cells(coord, reach) for o in on), f"{on} vs {reach:.4f}")
        check("knob on: blocks more than knob off", all(a > b for a, b in zip(on, off)),
              f"{off} -> {on}")

        fab_tiers.set_via_hole_clearance(None, None)
        check("nothing installed: no growth", cfg().via_hole_extra(0.127) == 0.0)
    finally:
        env_knobs.HOLE_FLOOR = old
        fab_tiers.set_via_hole_clearance(None, None)
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
