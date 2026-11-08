#!/usr/bin/env python3
"""NegotiatedRouter (rust_router/src/pathfinder.rs): the negotiated-congestion core on hand-built grids.

  A. Two nets that must cross, two layers: negotiation converges, both routed, copper legal (no foreign track
     centreline within r_tt on a layer, one via between them).
  B. A crossing on one layer between the grid's edges: no legal pair exists; legalise drops one net, the other stays, nothing is shared.
  C. Three nets through one corridor two lanes wide: two get through legally, one is dropped.
  D. Determinism: the same nets give the same paths with 1 thread and 8 threads, chunk 1 and chunk 16.
  E. A stop file that exists stops a negotiation that has conflicts (C's) after its first pass ("time limit").
  F. Hard keep-outs: a cell another owner's keep-out covers is never used.
  G. Async mode (work queue, commit checked against the commits since the search began): A and C hold.
  J. A plane tap (pad -> any plane cell nearby, through a via) negotiated with a signal over the pad.

Run with:  python tests/test_negotiated_router.py
"""
import math
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _p in ('rust_router', 'py_router'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import numpy as np  # noqa: E402
import grid_router  # noqa: E402

FAILS = []
R_TT = 1.5


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        FAILS.append(what)


def router(layers, w=40, h=21, blocked=(), threads=4, chunk=16, stop_file='', async_mode=False):
    base = grid_router.GridObstacleMap(layers)
    for x, y, l in blocked:
        base.add_blocked_cell(x, y, l)
    r = grid_router.NegotiatedRouter(base, 0, 0, w, h)
    r.set_params(via_cost=4.0, turn_cost=0.3, r_tt=R_TT, r_tv=1.5, r_vv=2.5, margin=3, threads=threads, chunk=chunk,
                 max_iters=40, time_limit_s=30.0, stop_file=stop_file, async_mode=async_mode)
    return r


def cells(paths):
    return {(x, y, l) for p in paths for (x, y, l) in p}


def min_gap(a, b):
    """Smallest same-layer centreline distance between two nets' paths."""
    best = math.inf
    for (x, y, l) in cells(a):
        for (u, v, m) in cells(b):
            if l == m:
                best = min(best, math.hypot(x - u, y - v))
    return best


def vias(paths):
    return sum(1 for p in paths for i in range(len(p) - 1) if p[i][:2] == p[i + 1][:2] and p[i][2] != p[i + 1][2])


def main():
    # A: crossing pair, two layers
    r = router(2)
    a = r.add_net(1, 1, [[(2, 6, 0)], [(37, 14, 0)]])
    b = r.add_net(2, 2, [[(2, 14, 0)], [(37, 6, 0)]])
    s = r.run()
    pa, pb = r.paths(a), r.paths(b)
    check(s['stopped'] == 'converged' and s['routed'] == 2 and not s['dropped'], f"A converged, both routed ({s['stopped']}, {s['routed']})")
    check(pa and pb and min_gap(pa, pb) > R_TT, f"A copper legal (gap {min_gap(pa, pb) if pa and pb else None})")
    check(pa and pb and vias(pa) + vias(pb) >= 1, 'A one net changes layer to cross')

    # B: crossing pair, one layer, terminals on the grid's left and right edges (no way round an end)
    r = router(1)
    a = r.add_net(1, 1, [[(0, 6, 0)], [(39, 14, 0)]])
    b = r.add_net(2, 2, [[(0, 14, 0)], [(39, 6, 0)]])
    s = r.run()
    kept = [p for p in (r.paths(a), r.paths(b)) if p]
    check(len(s['dropped']) == 1 and len(kept) == 1, f"B one net dropped, one kept (dropped {s['dropped']})")

    # C: corridor x 15..24 open only on rows 8..10 (two lanes at r_tt 1.5: rows 8 and 10)
    blocked = [(x, y, 0) for x in range(15, 25) for y in range(21) if not 8 <= y <= 10]
    r = router(1, blocked=blocked)
    ids = [r.add_net(10 + k, 10 + k, [[(2, 4 + 6 * k, 0)], [(37, 4 + 6 * k, 0)]]) for k in range(3)]
    s = r.run()
    kept = [r.paths(i) for i in ids if r.paths(i)]
    legal = all(min_gap(kept[i], kept[j]) > R_TT for i in range(len(kept)) for j in range(i + 1, len(kept)))
    check(len(kept) == 2 and len(s['dropped']) == 1 and legal, f"C two through the two-lane corridor, one dropped ({len(kept)} kept)")
    check(all(not ((x, y, l) in set(blocked)) for p in kept for (x, y, l) in cells(p)), 'C no path on a blocked cell')

    # D: determinism across threads and chunking
    def run(threads, chunk):
        r = router(2, threads=threads, chunk=chunk)
        ids = [r.add_net(20 + k, 20 + k, [[(2, 2 + 3 * k, 0)], [(37, 18 - 3 * k, 0)]]) for k in range(6)]
        r.run()
        return [r.paths(i) for i in ids]
    one, many = run(1, 16), run(8, 16)
    check(one == many, 'D same paths with 1 and 8 threads (chunk 16)')
    check(run(8, 1) == run(8, 1), 'D chunk 1 repeatable')

    # E: stop file
    with tempfile.TemporaryDirectory() as td:
        stop = os.path.join(td, 'stop')
        open(stop, 'w').close()
        r = router(1, blocked=blocked, stop_file=stop)
        for k in range(3):
            r.add_net(10 + k, 10 + k, [[(2, 4 + 6 * k, 0)], [(37, 4 + 6 * k, 0)]])
        s = r.run()
        check(s['stopped'] == 'time limit' and s['passes'] == 1 and s['routed'] == 0,
              f"E an existing stop file stops before any search ({s['stopped']}, {s['passes']} pass, {s['routed']} routed)")

    # F: owner keep-out: owner 9's keep-out covers column x=20 rows 0..14; net 1 (owner 1) must go round it
    r = router(1)
    wall = np.array([(20, y, 0) for y in range(15)], dtype=np.int32)
    r.add_keepout(9, wall, np.zeros((0, 2), dtype=np.int32))
    a = r.add_net(1, 1, [[(2, 5, 0)], [(37, 5, 0)]])
    s = r.run()
    pa = r.paths(a)
    check(bool(pa) and not any((20, y, 0) in cells(pa) for y in range(15)), 'F a foreign keep-out is never crossed')

    # G: async mode on A and C
    r = router(2, threads=8, async_mode=True)
    a = r.add_net(1, 1, [[(2, 6, 0)], [(37, 14, 0)]])
    b = r.add_net(2, 2, [[(2, 14, 0)], [(37, 6, 0)]])
    s = r.run()
    pa, pb = r.paths(a), r.paths(b)
    check(s['stopped'] == 'converged' and s['routed'] == 2 and bool(pa) and bool(pb) and min_gap(pa, pb) > R_TT,
          f"G async: crossing pair converged and legal ({s['stopped']}, {s['routed']})")
    r = router(1, blocked=blocked, threads=8, async_mode=True)
    ids = [r.add_net(10 + k, 10 + k, [[(2, 4 + 6 * k, 0)], [(37, 4 + 6 * k, 0)]]) for k in range(3)]
    s = r.run()
    kept = [r.paths(i) for i in ids if r.paths(i)]
    legal = all(min_gap(kept[i], kept[j]) > R_TT for i in range(len(kept)) for j in range(i + 1, len(kept)))
    check(len(kept) == 2 and legal, f"G async: two through the two-lane corridor, legal ({len(kept)} kept)")

    # H: forbidden layer (KRT layer cost -1): layer 1 carries no track; A's crossing pair must go round on layer 0
    r = router(2)
    r.set_layer_forbidden([False, True])
    a = r.add_net(1, 1, [[(2, 6, 0)], [(37, 14, 0)]])
    b = r.add_net(2, 2, [[(2, 14, 0)], [(37, 6, 0)]])
    s = r.run()
    pa, pb = r.paths(a), r.paths(b)
    on1 = [c for c in cells((pa or []) + (pb or [])) if c[2] == 1]
    check(s['routed'] == 2 and bool(pa) and bool(pb) and not on1 and min_gap(pa, pb) > R_TT,
          f"H forbidden layer carries no track ({s['routed']} routed, {len(on1)} cells on layer 1)")
    # a terminal on the forbidden layer (a through-hole pad on a plane layer) is still reached, by a via at the pad
    r = router(2)
    r.set_layer_forbidden([False, True])
    a = r.add_net(1, 1, [[(2, 6, 1)], [(37, 6, 0)]])
    s = r.run()
    pa = r.paths(a)
    on1 = {c for c in cells(pa or []) if c[2] == 1}
    check(bool(pa) and on1 <= {(2, 6, 1)}, f"H terminal on the forbidden layer reached only at the pad (layer-1 cells {sorted(on1)})")

    # I: a wide net (extra 2 cells each side) keeps r_tt + extra from a default net and extra from hard copper.
    # The wall pushes the wide net from row 9 to row 10 over x 10..29; the default net on row 13 must then dip to 14.
    wall = [(x, 7, 0) for x in range(10, 30)]
    r = router(1, blocked=wall)
    wnet = r.add_net(1, 1, [[(2, 9, 0)], [(37, 9, 0)]], extra=2.0)
    nnet = r.add_net(2, 2, [[(2, 13, 0)], [(37, 13, 0)]])
    s = r.run()
    pw, pn = r.paths(wnet), r.paths(nnet)
    wall_gap = min((math.hypot(x - u, y - v) for (x, y, l) in cells(pw or []) for (u, v, m) in wall), default=math.inf)
    check(s['routed'] == 2 and bool(pw) and bool(pn) and min_gap(pw, pn) >= R_TT + 2.0,
          f"I wide net keeps r_tt + extra from its neighbour (gap {min_gap(pw, pn) if pw and pn else None})")
    check(bool(pw) and wall_gap >= 2.0, f"I wide net keeps extra from hard copper (gap {wall_gap:.2f})")
    check(s['stopped'] == 'converged' and not s['dropped'], f"I converged with nothing dropped ({s['stopped']}, {s['passes']} passes)")
    rep = r.terminal_report(wnet)
    check(len(rep) == 2 and all(t[0] == 1 and t[2] == 1 for t in rep), f"I terminal_report sees free terminals ({rep})")

    # J: a plane tap (KRT_NEGOTIATED_PLANE_TAPS): the pad on layer 0, the plane terminal every cell of layer 1 (the
    # forbidden plane layer) within 4 cells of it. A signal crossing right over the pad competes for the via site;
    # the tap ends on the plane through one via with no step along the plane, and both are legal.
    r = router(2)
    r.set_layer_forbidden([False, True])
    pad = (20, 10)
    plane = [(pad[0] + dx, pad[1] + dy, 1) for dx in range(-4, 5) for dy in range(-4, 5) if dx * dx + dy * dy <= 16]
    tap = r.add_net(-1, 7, [[(pad[0], pad[1], 0)], plane])
    sig = r.add_net(2, 2, [[(2, 10, 0)], [(37, 10, 0)]])
    s = r.run()
    pt, ps = r.paths(tap), r.paths(sig)
    on1 = [c for c in cells(pt or []) if c[2] == 1]
    check(s['routed'] == 2 and not s['dropped'] and bool(pt) and bool(ps), f"J tap and signal both routed ({s['routed']}, dropped {s['dropped']})")
    check(len(on1) == 1 and on1[0] in set(plane), f"J tap reaches the plane at one cell through a via ({on1})")
    check(bool(pt) and bool(ps) and min_gap([[c for p in pt for c in p if c[2] == 0]], ps) >= R_TT,
          f"J tap keeps r_tt from the signal on layer 0 (gap {min_gap(pt, ps) if pt and ps else None})")

    print(f'\n{len(FAILS)} failure(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
