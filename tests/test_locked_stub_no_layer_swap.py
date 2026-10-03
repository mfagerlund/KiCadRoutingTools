#!/usr/bin/env python3
"""
Regression test: KiCad-locked copper never moves layer.

A locked full-width supply pre-route on B.Cu was relayered onto In2.Cu by the
main pass's stub layer swaps, where the thinner inner copper made it fail its
current-width check. Locked copper is already protected from rip-up (#521);
the swap path (get_stub_info -> apply_stub_layer_switch, the only place a
segment's layer is changed in place) ignored the flag.

  - get_stub_info returns None for a stub containing a locked segment
  - apply_stub_layer_switch leaves locked copper on its layer even when a
    caller hands it a StubInfo built by hand
  - an otherwise identical unlocked stub still swaps (no over-rejection)

Run:
    python3 tests/test_locked_stub_no_layer_swap.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))

from kicad_parser import PCBData, Segment, Pad, Net
from routing_config import GridRouteConfig
from stub_layer_switching import get_stub_info, apply_stub_layer_switch

NET = 1


def _cfg():
    c = GridRouteConfig()
    c.layers = ['F.Cu', 'In2.Cu', 'B.Cu']
    c.track_width = 0.12
    c.clearance = 0.12
    c.via_size = 0.45
    c.via_drill = 0.2
    return c


def _pcb(locked):
    pad = Pad(component_ref='J1', pad_number='1', global_x=0.0, global_y=0.0,
              local_x=0.0, local_y=0.0, size_x=1.0, size_y=1.0, shape='circle',
              layers=['F.Cu', 'B.Cu'], net_id=NET, net_name='SUPPLY', drill=0.6)
    far = Pad(component_ref='U1', pad_number='1', global_x=10.0, global_y=10.0,
              local_x=0.0, local_y=0.0, size_x=0.5, size_y=0.5, shape='rect',
              layers=['F.Cu'], net_id=NET, net_name='SUPPLY')
    segs = [Segment(start_x=0.0, start_y=0.0, end_x=2.0, end_y=0.0, width=0.35,
                    layer='B.Cu', net_id=NET, locked=locked),
            Segment(start_x=2.0, start_y=0.0, end_x=3.0, end_y=1.0, width=0.35,
                    layer='B.Cu', net_id=NET, locked=locked)]
    return PCBData(footprints={}, segments=segs, vias=[],
                   nets={NET: Net(NET, 'SUPPLY')}, board_info=None,
                   pads_by_net={NET: [pad, far]})


def main():
    cfg = _cfg()
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}: {name}")
        if not cond:
            fails.append(name)

    pcb = _pcb(locked=True)
    check("locked stub is not a swap candidate", get_stub_info(pcb, NET, 3.0, 1.0, 'B.Cu') is None)

    free = _pcb(locked=False)
    stub = get_stub_info(free, NET, 3.0, 1.0, 'B.Cu')
    check("unlocked stub is a swap candidate", stub is not None)

    # hand-built StubInfo pointing at locked copper: apply must refuse
    for seg in free.segments:
        seg.locked = True
    vias, mods = apply_stub_layer_switch(free, stub, 'In2.Cu', cfg, debug=False)
    check("apply leaves locked copper on its layer",
          not mods and not vias and all(s.layer == 'B.Cu' for s in free.segments))

    for seg in free.segments:
        seg.locked = False
    vias, mods = apply_stub_layer_switch(free, stub, 'In2.Cu', cfg, debug=False)
    check("unlocked stub still switches", mods and all(s.layer == 'In2.Cu' for s in free.segments))

    if fails:
        print(f"\nFAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("\nPASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
