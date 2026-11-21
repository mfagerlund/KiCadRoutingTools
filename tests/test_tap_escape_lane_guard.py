#!/usr/bin/env python3
"""A plane tap's new via stays out of neighbour pads' escape lanes.

An HDMI connector (0.5 mm pitch, 1.9 x 0.3 mm pads) interleaves GND with the
TMDS pairs. A rip-up plane tap of a GND pin placed its via 0.49 mm past the end
of the neighbouring TMDS pad, in that pad's only escape lane; the TMDS reconnect
then failed at that pad. A fine-pitch QFN power pin was walled in the same way.

Checks (try_tap_pad(plane_tap=True), as repair_planes calls it, on a synthetic
board: a 3-pad row SIG_A / GND / SIG_B at 0.5 mm pitch on the east side of a
footprint, foreign copper behind the row, GND poured on B.Cu):
  1. The via lands outside every foreign neighbour's outward lane.
  2. A guard that rejects every site falls back to the unguarded search, so
     the tap still succeeds.
  3. Neighbours of the tap's own net set no lane: the via sits where it did.

    python3 tests/test_tap_escape_lane_guard.py
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from kicad_parser import Pad, Segment, Zone, PCBData, BoardInfo, Footprint   # noqa: E402
from routing_config import GridRouteConfig                          # noqa: E402
from plane_pad_tap import try_tap_pad, escape_lane_site_filter      # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


PAD_X, PAD_LEN, PAD_W = 12.0, 1.9, 0.3
PAD_END = PAD_X + PAD_LEN / 2


def board(neighbour_nets=((2, 'SIG_A'), (3, 'SIG_B'))):
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'}, copper_layers=['F.Cu', 'B.Cu'])
    bi.board_bounds = (0.0, 0.0, 24.0, 20.0)

    def mk(num, y, nid, name):
        return Pad(component_ref='J1', pad_number=num, global_x=PAD_X, global_y=y,
                   local_x=PAD_X - 10.0, local_y=y - 10.0, size_x=PAD_LEN,
                   size_y=PAD_W, shape='rect', layers=['F.Cu'], net_id=nid,
                   net_name=name, pad_type='smd')
    (na, sa), (nb, sb) = neighbour_nets
    pa, gnd, pb = mk('7', 9.5, na, sa), mk('8', 10.0, 1, 'GND'), mk('9', 10.5, nb, sb)
    fp = Footprint(reference='J1', footprint_name='conn', x=10.0, y=10.0,
                   rotation=0.0, layer='F.Cu', pads=[pa, gnd, pb])
    poly = [(2.0, 2.0), (22.0, 2.0), (22.0, 18.0), (2.0, 18.0)]
    zones = [Zone(net_id=1, net_name='GND', layer='B.Cu', polygon=list(poly))]
    by_net = {}
    for p in (pa, gnd, pb):
        by_net.setdefault(p.net_id, []).append(p)
    # The connector body: foreign F.Cu copper behind the row keeps the via
    # off the inward side, as a real footprint's other pads and parts do.
    body = [Segment(start_x=10.6, start_y=7.0, end_x=10.6, end_y=13.0,
                    width=0.6, layer='F.Cu', net_id=4)]
    return gnd, PCBData(board_info=bi, nets={}, footprints={'J1': fp}, vias=[],
                        segments=body, pads_by_net=by_net, zones=zones)


cfg = GridRouteConfig(track_width=0.2, clearance=0.15, via_size=0.6,
                      via_drill=0.3, grid_step=0.05, board_edge_clearance=0.2,
                      hole_to_hole_clearance=0.2, layers=['F.Cu', 'B.Cu'])


def tap(pad, pcb, radius=3.0):
    with contextlib.redirect_stdout(io.StringIO()):
        return try_tap_pad(pad, 'F.Cu', 1, pcb, cfg, max_search_radius=radius,
                           via_size=0.6, via_drill=0.3, disable_reuse=True,
                           plane_tap=True)


def in_lane(x, y):
    """Centre inside the outward lane of pad 7 (y 9.5) or pad 9 (y 10.5)."""
    half = cfg.track_width / 2 + cfg.clearance + 0.6 / 2
    t = x - PAD_END
    return 0.0 <= t <= 2 * 0.6 and any(abs(y - ly) < half for ly in (9.5, 10.5))


print("1. a GND tap between two signal pads")
pad, pcb = board()
r = tap(pad, pcb)
via = r.via or {}
check("tapped with a via", r.success and r.via is not None, f"via={r.via}")
check("the via is outside both neighbours' escape lanes",
      bool(via) and not in_lane(via['x'], via['y']),
      f"via at ({via.get('x')}, {via.get('y')})")

print("2. a guard that leaves no site")
import plane_pad_tap                                                # noqa: E402
_real = plane_pad_tap.escape_lane_site_filter
plane_pad_tap.escape_lane_site_filter = lambda *a, **k: (lambda x, y: False)
try:
    pad, pcb = board()
    r = tap(pad, pcb)
finally:
    plane_pad_tap.escape_lane_site_filter = _real
check("the tap falls back to the unguarded search and succeeds",
      r.success and r.via is not None, f"via={r.via}")

print("3. same-net neighbours set no lane")
pad, pcb = board(neighbour_nets=((1, 'GND'), (1, 'GND')))
check("no filter for same-net neighbours",
      escape_lane_site_filter(pad, pcb, 1, 0.6, cfg) is None)

if failures:
    print(f"\nFAILED: {failures}")
    sys.exit(1)
print("\nALL PASSED")
