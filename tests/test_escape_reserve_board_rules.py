#!/usr/bin/env python3
"""The escape reserve's dog-bones obey the board's own rules, not only the
pin's clearance and the bounding box.

The planned stubs and vias go into the obstacle maps as fixed copper and ship
when their net routes, so every site must already be legal:

  1. a keep-out rule area that forbids vias holds no planned via (ring
     inside or overlapping it), and one that forbids tracks holds no stub
     on its layer;
  2. against a pad of a net whose netclass clearance is larger than the
     pin's, a via keeps that larger clearance (KiCad grades the pair at the
     larger of the two);
  3. a board cutout (a hole in the outline) holds no via.

Fixture: the fine-pitch row of test_NNNN_escape_reserve_qfn (a column of
0.5 mm pitch pads, a wall pad of another net across a strip in front of it).

    python3 tests/test_escape_reserve_board_rules.py
"""
import contextlib
import io
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import test_NNNN_escape_reserve_qfn as qfn  # noqa: E402


def _load():
    from kicad_parser import parse_kicad_pcb
    d = tempfile.mkdtemp(prefix='er_rules_')
    p = os.path.join(d, 'b.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(qfn.board_text())
    with contextlib.redirect_stdout(io.StringIO()):
        pcb = parse_kicad_pcb(p)
    return pcb


def _plan(pcb, cfg):
    import escape_reserve as er
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    with contextlib.redirect_stdout(io.StringIO()):
        planned, _unplaced = er.plan(pcb, cfg, ids)
    vias = [it[1] for items in planned.values() for it in items]
    stubs = [(it[2], it[3], it[4]) for items in planned.values() for it in items]
    return vias, stubs


def _box(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _ring_hits_box(x, y, r, box):
    (x0, y0), _, (x1, y1), _ = box
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx, dy) < r - 1e-9


def _seg_hits_box(pts, hw, box):
    (x0, y0), _, (x1, y1), _ = box
    for a, b in zip(pts, pts[1:]):
        for k in range(21):
            t = k / 20
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            if _ring_hits_box(x, y, hw, box):
                return True
    return False


def _area_of(vias):
    xs = [v[0] for v in vias]
    ys = [v[1] for v in vias]
    return min(xs), min(ys), max(xs), max(ys)


def test_via_keepout_holds_no_via():
    pcb = _load()
    cfg = qfn._config()
    vias, _ = _plan(pcb, cfg)
    assert vias, 'fixture: the row must get dog-bones'
    x0, y0, x1, y1 = _area_of(vias)
    ym = (y0 + y1) / 2
    box = _box(x0 - 0.1, y0 - 0.1, x1 + 0.1, ym)   # the upper half of the sites
    pcb.board_info.keepouts = [{'polygon': box, 'layers': {'*.Cu'},
                                'tracks_allowed': True, 'vias_allowed': False}]
    vias, _ = _plan(pcb, cfg)
    bad = [v for v in vias if _ring_hits_box(v[0], v[1], cfg.via_size / 2, box)]
    assert not bad, f'vias in a no-via keep-out: {bad}'


def test_track_keepout_holds_no_stub():
    pcb = _load()
    cfg = qfn._config()
    _, stubs = _plan(pcb, cfg)
    pts = [p for s in stubs for p in s[0]]
    x0, y0, x1, y1 = _area_of(pts)
    box = _box(x0 - 0.1, y0 - 0.1, x1 + 0.1, (y0 + y1) / 2)
    pcb.board_info.keepouts = [{'polygon': box, 'layers': {'F.Cu'},
                                'tracks_allowed': False, 'vias_allowed': True}]
    _, stubs = _plan(pcb, cfg)
    # The stub's pad end lies on the pad itself; only the escape beyond the
    # pad edge counts (the keep-out does not cover the pads: x0 is the pad).
    bad = [s for s in stubs if s[1] == 'F.Cu'
           and _seg_hits_box([p for p in s[0] if p[0] > x0 + 1e-6] or s[0][-1:],
                             s[2] / 2, box)]
    assert not bad, f'stubs in a no-track keep-out: {bad}'


def test_other_net_clearance_binds():
    pcb = _load()
    cfg = qfn._config()
    wall = next(nid for nid, n in pcb.nets.items() if n.name == 'WALL')
    big = 0.45
    cfg.net_clearances = {wall: big}
    vias, _ = _plan(pcb, cfg)
    assert vias, 'fixture: the row must still get dog-bones'
    import escape_reserve as er
    wpad = pcb.pads_by_net[wall][0]
    short = [v for v in vias
             if er._pad_dist_pt(wpad, v[0], v[1]) < cfg.via_size / 2 + big - 1e-6]
    assert not short, f'vias closer to WALL than its clearance {big}: {short}'


def test_cutout_holds_no_via():
    pcb = _load()
    cfg = qfn._config()
    vias, _ = _plan(pcb, cfg)
    x0, y0, x1, y1 = _area_of(vias)
    box = _box(x0 - 0.1, y0 - 0.1, x1 + 0.1, (y0 + y1) / 2)
    pcb.board_info.board_cutouts = [box]
    vias, _ = _plan(pcb, cfg)
    bad = [v for v in vias if _ring_hits_box(v[0], v[1], cfg.via_size / 2, box)]
    assert not bad, f'vias in a board cutout: {bad}'


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as e:
                fails += 1
                print(f'FAIL {name}: {e}')
    sys.exit(1 if fails else 0)
