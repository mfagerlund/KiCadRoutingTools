#!/usr/bin/env python3
"""Stored widths, clearances, drills and diameters keep their floor.

Every value the router computes and keeps goes through
`routing_utils.stored_value`: on KiCad's 1 nm grid, never below its floor,
never above its cap. A 7.8 mil Board Setup minimum (0.19812 mm) used to ship
as 0.1981 from the stages that rounded to 0.1 um (the cleanup passes'
width groups, the plane-pad tap clearance ladder, the plane neck).

  1. the helper: grid, floor, cap, floor over cap;
  2. the writer emits 0.19812, not 0.1981;
  3. merge_collinear_segments and the plane-pad tap ladder keep 0.19812;
  4. KICAD_HOLE_FLOOR holds a foreign track off a thin-ring plated pad hole
     at the board's hole clearance (the ring + clearance falls short of it),
     and leaves a thick-ring pad and the knob-off run alone.

    python3 tests/test_stored_value_floor.py
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env_knobs  # noqa: E402
import fab_tiers  # noqa: E402
from routing_utils import stored_value, neck_round  # noqa: E402

FLOOR = 0.19812
FAILS = []


def check(name, cond, detail=''):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ''))
    if not cond:
        FAILS.append(name)


def on_grid(v):
    return abs(v * 1e6 - round(v * 1e6)) < 1e-6


def helper():
    check("rounds to 1 nm", stored_value(0.1981200000004) == FLOOR)
    check("floor holds", stored_value(0.1981, floor=FLOOR) == FLOOR)
    check("floor off-grid rounds up", stored_value(0.1, floor=0.1999994) == 0.2)
    check("cap rounds down", stored_value(0.3000006, cap=0.3000006) == 0.3)
    check("floor beats cap", stored_value(0.1, floor=0.2, cap=0.15) == 0.2)
    check("neck_round is the helper", neck_round(0.198119, FLOOR) == FLOOR)


def writer():
    from kicad_writer import generate_segment_sexpr
    s = generate_segment_sexpr((0, 0), (1, 0), FLOOR, 'F.Cu', 1)
    check("writer keeps 0.19812", '(width 0.19812)' in s, s.split('\n')[3].strip())
    s = generate_segment_sexpr((0, 0), (1, 0), 0.1 + 0.2, 'F.Cu', 1)
    check("writer on the grid", '(width 0.3)' in s, s.split('\n')[3].strip())


def cleanup_merge():
    from pcb_modification import merge_collinear_segments
    from synth import make_pcb, make_seg
    segs = [make_seg(0, 0, 1, 0, width=FLOOR), make_seg(1, 0, 2, 0, width=FLOOR)]
    pcb = make_pcb(segments=list(segs), vias=[], pads_by_net={})
    merge_collinear_segments([], pcb, None)
    ws = {s.width for s in pcb.segments}
    check("merge keeps 0.19812", ws == {FLOOR}, str(ws))
    check("merge joined the run", len(pcb.segments) == 1, str(len(pcb.segments)))


def tap_ladder():
    from plane_pad_tap import _clearance_ladder
    lad = _clearance_ladder(0.2, FLOOR, 3)
    check("tap ladder ends on the floor", lad[-1] == FLOOR, str(lad))
    check("tap ladder never under the floor", all(v >= FLOOR for v in lad), str(lad))
    check("tap ladder on the grid", all(on_grid(v) for v in lad), str(lad))


def hole_floor():
    from obstacle_map import stamp_hole_rule_pad_holes
    from routing_config import GridRouteConfig
    from synth import make_pad

    class Rec:
        def __init__(self):
            self.cells = []

        def add_blocked_cells_batch(self, arr):
            self.cells.extend(map(tuple, arr.tolist()))

        def add_blocked_vias_batch(self, arr):
            pass

    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.001, clearance=0.127,
                          track_width=0.127, via_size=0.6, via_drill=0.3)
    thin = make_pad(net_id=7, x=10.0, y=10.0, size_x=0.4, size_y=0.4, shape='circle',
                    drill=0.3, pad_type='thru_hole', layers=('*.Cu',))
    thick = make_pad(net_id=7, x=10.0, y=10.0, size_x=1.0, size_y=1.0, shape='circle',
                     drill=0.3, pad_type='thru_hole', layers=('*.Cu',))
    # hackrf-pro U40 pad 7: a track 0.1785 off the hole (board hole clearance
    # 0.18) clears the copper ring by more than 0.127, so only the hole rule
    # holds it off.
    probe = (10.0 + 0.15 + 0.1785 + 0.127 / 2, 10.0)
    check("probe clears the ring copper", probe[0] - 10.0 - 0.2 - 0.127 / 2 >= 0.127)
    old = env_knobs.HOLE_FLOOR
    fab_tiers.set_via_hole_clearance(0.18, 0.127)
    try:
        def run(pad, knob):
            env_knobs.HOLE_FLOOR = knob
            r = Rec()
            stamp_hole_rule_pad_holes(r, [pad], cfg)
            return r
        gx, gy = round(probe[0] / cfg.grid_step), round(probe[1] / cfg.grid_step)
        on = run(thin, True)
        check("knob on: probe cell blocked", (gx, gy, 0) in set(on.cells),
              f"{len(on.cells)} cells")
        reach = max(math.hypot(x * cfg.grid_step - 10.0, y * cfg.grid_step - 10.0)
                    for x, y, _ in on.cells)
        check("knob on: reach covers hole + 0.18 + w/2",
              reach >= 0.15 + 0.18 + 0.127 / 2 - cfg.grid_step, f"{reach:.4f}")
        check("knob off: nothing stamped", not run(thin, False).cells)
        check("thick ring: nothing stamped", not run(thick, True).cells)
    finally:
        env_knobs.HOLE_FLOOR = old
        fab_tiers.set_via_hole_clearance(None, None)


def main():
    helper()
    writer()
    cleanup_merge()
    tap_ladder()
    hole_floor()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
