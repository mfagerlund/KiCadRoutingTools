#!/usr/bin/env python3
"""Walled-net rip-and-reroute (walled_rip.py): a net still open after the
late passes because another net's copper walls its pad is rerouted by
ripping the walling net, routing the open net and rerouting the victim --
all or nothing.

Single-layer fixture: net N's pad N.1 sits low on the board, enclosed by net
W's arch (W.1 -> up -> across -> down -> W.2) and the board edge; N.2 sits at the top edge, so
nothing passes it. No route for N exists while the arch stands; W has a direct
route along the bottom, under N.1, once N leaves upward.

  1. The pass finds W as the wall, rips it, routes N, reroutes W: both nets
     connected, the new copper clears all foreign copper, the result
     channels carry both nets once.
  2. Locked W copper is never ripped: N stays open, the board is unchanged.
  3. A wall whose victim cannot come back (N.1 between W's pads at the
     edge, so N's route separates them) is rolled back: N open, W's arch
     restored item for item.
  4. KICAD_WALLED_RIP=0 disables the pass.

    python3 tests/test_walled_net_rip.py
"""
import contextlib
import io
import math
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, TESTS_DIR)

WIDTH = 0.2
CLEARANCE = 0.2


def board_text(n1_xy, locked=False):
    lk = ' (locked yes)' if locked else ''
    arch = [(8, 14), (8, 7), (22, 7), (22, 14)]
    segs = ''.join(
        f'\n  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {WIDTH})'
        f' (layer "F.Cu") (net 2){lk} (uuid "bbbbbbbb-0000-0000-0000-00000000000{i}"))'
        for i, (a, b) in enumerate(zip(arch, arch[1:])))
    return f"""(kicad_pcb (version 20241229) (generator "pcbnew")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "N")
  (net 2 "W")
  (footprint "test:pad" (layer "F.Cu") (at {n1_xy[0]} {n1_xy[1]})
    (property "Reference" "N1" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 1 "N")))
  (footprint "test:pad" (layer "F.Cu") (at 15 0.4)
    (property "Reference" "N2" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 1 "N")))
  (footprint "test:pad" (layer "F.Cu") (at 8 14)
    (property "Reference" "W1" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "W")))
  (footprint "test:pad" (layer "F.Cu") (at 22 14)
    (property "Reference" "W2" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "W"))){segs}
  (gr_rect (start 0 0) (end 30 14.5) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))
)
"""


failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def make_state(tmp, n1_xy, locked=False):
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    from routing_state import RoutingState
    path = os.path.join(tmp, 'walled.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(board_text(n1_xy, locked))
    with contextlib.redirect_stdout(io.StringIO()):
        pcb = parse_kicad_pcb(path)
    ids = {n.name: i for i, n in pcb.nets.items()}
    cfg = GridRouteConfig(grid_step=0.1, clearance=CLEARANCE, track_width=WIDTH,
                          via_size=0.45, via_drill=0.2, layers=['F.Cu'])
    state = RoutingState(pcb_data=pcb, config=cfg)
    w = ids['W']
    res = {'net_id': w, 'net_name': 'W', 'iterations': 1,
           'new_segments': [s for s in pcb.segments if s.net_id == w],
           'new_vias': []}
    state.routed_results[w] = res
    state.results.append(res)
    state.routed_net_ids.append(w)
    state.remaining_net_ids.append(ids['N'])
    return state, ids


def run_pass(state, ids):
    from walled_rip import walled_net_rip
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        summary = walled_net_rip(state, [('N', ids['N'])])
    return summary, log.getvalue()


def parts(state, nid):
    from net_rescue import _net_component_info
    return _net_component_info(state.pcb_data, nid)[0]


def _seg_dist(a, b):
    def pt(p, s):
        (x1, y1, x2, y2) = s
        dx, dy = x2 - x1, y2 - y1
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / L))
        return math.hypot(p[0] - x1 - t * dx, p[1] - y1 - t * dy)
    return min(pt(a[:2], b), pt(a[2:], b), pt(b[:2], a), pt(b[2:], a))


def min_gap(state, n, w):
    def xs(nid):
        return [(s.start_x, s.start_y, s.end_x, s.end_y)
                for s in state.pcb_data.segments if s.net_id == nid]
    gaps = [_seg_dist(a, b) - WIDTH for a in xs(n) for b in xs(w)]
    return min(gaps) if gaps else float('inf')


def copper_sig(state, nid):
    return sorted((round(s.start_x, 3), round(s.start_y, 3), round(s.end_x, 3),
                   round(s.end_y, 3)) for s in state.pcb_data.segments
                  if s.net_id == nid)


def main():
    import env_knobs
    with tempfile.TemporaryDirectory(prefix='twalled_') as tmp:
        print("1. N.1 walled by W's arch: rip W, route N, reroute W")
        state, ids = make_state(tmp, (15, 11))
        n, w = ids['N'], ids['W']
        check("fixture: N open, W connected", parts(state, n) > 1 and parts(state, w) == 1)
        summary, log = run_pass(state, ids)
        check("the pass names W as the wall", 'ripping W' in log, log.strip()[-200:])
        check("N recovered", parts(state, n) == 1 and summary
              and summary['nets'].get('N', '').startswith('recovered'),
              repr(summary))
        check("W rerouted and connected", parts(state, w) == 1)
        gap = min_gap(state, n, w)
        check("N and W copper clear each other", gap >= CLEARANCE - 1e-6, f"gap {gap:.3f}")
        check("one result per net", sorted(r['net_id'] for r in state.results) == sorted([n, w])
              and n in state.routed_results and w in state.routed_results)
        check("N left the remaining list", n not in state.remaining_net_ids)

        print("2. locked W copper is never ripped")
        state, ids = make_state(tmp, (15, 11), locked=True)
        before = copper_sig(state, ids['W'])
        summary, log = run_pass(state, ids)
        check("N stays open", parts(state, ids['N']) > 1)
        check("W copper untouched", copper_sig(state, ids['W']) == before)
        check("nothing attempted", summary is None, repr(summary))

        print("3. a victim that cannot come back rolls everything back")
        state, ids = make_state(tmp, (15, 13.9))
        before = copper_sig(state, ids['W'])
        summary, log = run_pass(state, ids)
        check("N stays open", parts(state, ids['N']) > 1)
        check("W's arch restored", copper_sig(state, ids['W']) == before
              and parts(state, ids['W']) == 1)
        check("no N copper left", copper_sig(state, ids['N']) == [])
        check("W keeps one result", [r['net_id'] for r in state.results] == [ids['W']])
        check("outcome is rolled back",
              bool(summary) and summary['nets'].get('N') == 'rolled back', repr(summary))

        print("4. KICAD_WALLED_RIP=0 disables the pass")
        state, ids = make_state(tmp, (15, 11))
        saved = env_knobs.WALLED_RIP
        env_knobs.WALLED_RIP = False
        try:
            summary, _ = run_pass(state, ids)
        finally:
            env_knobs.WALLED_RIP = saved
        check("no pass, N open", summary is None and parts(state, ids['N']) > 1)
    if failures:
        print(f"\nFAIL: {len(failures)} check(s): {failures}")
        return 1
    print("\nPASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
