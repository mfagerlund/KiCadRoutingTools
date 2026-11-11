"""grid_router.seg_dist_need / pad_dist_need decide `dist >= need` exactly as the numpy path of
_seg_foreign_seg_dist / _seg_foreign_pad_dist, and return numpy's value bit for bit below need: random boards of
foreign tracks, vias and rounded-rect pads, with and without per-net clearances."""
import math
import os
import random
import sys
from types import SimpleNamespace as NS

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

import single_ended_routing as ser  # noqa: E402



def _agree(r, ref, need):
    return (r < need) == (ref < need) and (ref >= need or r == ref)


def test_seg_dist_need_matches_numpy():
    rnd = random.Random(11)
    calls = 0
    for trial in range(60):
        segs = []
        for _ in range(rnd.randint(1, 60)):
            x, y = rnd.uniform(0, 10), rnd.uniform(0, 10)
            a = rnd.choice([0, 45, 90, 135, 17]) * math.pi / 180
            ln = rnd.choice([0.0, rnd.uniform(0, 4)])
            segs.append(NS(start_x=x, start_y=y, end_x=x + ln * math.cos(a), end_y=y + ln * math.sin(a),
                           layer=rnd.choice(['F.Cu', 'B.Cu']), width=rnd.choice([0.1, 0.13, 0.25]),
                           net_id=rnd.randint(1, 6)))
        vias = [NS(x=rnd.uniform(0, 10), y=rnd.uniform(0, 10), size=0.45, drill=0.2, net_id=rnd.randint(1, 6),
                   layers=['F.Cu', 'B.Cu']) for _ in range(rnd.randint(0, 10))]
        pcb = NS(segments=segs, vias=vias, footprints=None)
        nc = {k: rnd.choice([0.1, 0.15, 0.2]) for k in range(1, 7)} if trial % 2 else None
        for _ in range(20):
            x1, y1 = rnd.uniform(1, 9), rnd.uniform(1, 9)
            x2, y2 = (x1, y1) if rnd.random() < 0.1 else (x1 + rnd.uniform(-3, 3), y1 + rnd.uniform(-3, 3))
            net = rnd.randint(1, 6)
            need = rnd.choice([0.0, 0.05, 0.1, 0.2, 0.5])
            r = ser._seg_foreign_seg_dist(pcb, net, x1, y1, x2, y2, 'F.Cu', net_clearances=nc,
                                          base_clearance=0.1, need=need)
            ref = ser._seg_foreign_seg_dist_numpy(pcb, net, x1, y1, x2, y2, 'F.Cu', nc, 0.1, None, need)
            assert _agree(r, ref, need), (trial, r, ref, need)
            calls += 1
    assert calls == 1200


def _pads(rnd, n):
    nids = np.array([rnd.randint(1, 6) for _ in range(n)])
    cx = np.array([rnd.uniform(0, 10) for _ in range(n)])
    cy = np.array([rnd.uniform(0, 10) for _ in range(n)])
    hx = np.array([rnd.uniform(0.1, 1.0) for _ in range(n)])
    hy = np.array([rnd.uniform(0.1, 1.0) for _ in range(n)])
    cr = np.array([rnd.choice([0.0, 0.05, min(a, b)]) for a, b in zip(hx, hy)])
    rot = np.array([rnd.choice([0.0, 45.0, 30.0, 90.0]) * math.pi / 180 for _ in range(n)])
    rc, rs = np.cos(rot), np.sin(rot)
    ex = np.abs(hx * rc) + np.abs(hy * rs)
    ey = np.abs(hx * rs) + np.abs(hy * rc)
    plc = np.array([rnd.choice([0.0, 0.0, 0.2]) for _ in range(n)])
    return nids, cx, cy, hx, hy, cr, rc, rs, ex, ey, plc, []


def test_pad_dist_need_matches_numpy():
    saved = ser._foreign_pad_arrays
    try:
        _pad_trials()
    finally:
        ser._foreign_pad_arrays = saved


def _pad_trials():
    rnd = random.Random(13)
    for trial in range(60):
        arr = _pads(rnd, rnd.randint(1, 50))
        ser._foreign_pad_arrays = lambda pcb, layer, _a=arr: _a
        nc = {k: rnd.choice([0.1, 0.15, 0.3]) for k in range(1, 7)} if trial % 2 else None
        bc = None if trial % 3 == 0 else 0.1
        for _ in range(20):
            x1, y1 = rnd.uniform(1, 9), rnd.uniform(1, 9)
            x2, y2 = (x1, y1) if rnd.random() < 0.1 else (x1 + rnd.uniform(-3, 3), y1 + rnd.uniform(-3, 3))
            net = rnd.randint(1, 6)
            need = rnd.choice([0.0, 0.05, 0.1, 0.2, 0.5])
            r = ser._seg_foreign_pad_dist(None, net, x1, y1, x2, y2, 'F.Cu', base_clearance=bc,
                                          net_clearances=nc, need=need)
            ref = ser._seg_foreign_pad_dist_numpy(None, net, x1, y1, x2, y2, 'F.Cu', bc, nc, need)
            assert _agree(r, ref, need), (trial, r, ref, need)


if __name__ == '__main__':
    if ser._RUST_SEG_NEED is None or ser._RUST_PAD_NEED is None:
        print('SKIP: grid_router built without the clearance kernels')
        sys.exit(77)
    test_seg_dist_need_matches_numpy()
    test_pad_dist_need_matches_numpy()
    print('PASS')
