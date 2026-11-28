#!/usr/bin/env python3
"""The escape reserve's legality tests cost what is near a site, not what is
on the board.

Each candidate via and stub is checked against the board outline and every
pad. On a board with a finely segmented outline (arcs flattened to hundreds
of points) and a thousand pads the prepass spent over a minute measuring
every outline edge and every pad's extents for each of thousands of
candidates. The plan must be the same, and:

  1. outline edges far from every site add no segment distance computations
     (a 2000-point outline costs what a 200-point one does);
  2. pads far from every site are read once per plan, not once per test.

Fixture: the fine-pitch row of test_NNNN_escape_reserve_qfn, with a large
round outline around it and a field of distant pads added.

    python3 tests/test_escape_reserve_scale.py
"""
import copy
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import test_escape_reserve_board_rules as rules  # noqa: E402
import test_NNNN_escape_reserve_qfn as qfn  # noqa: E402


def _centre(pcb):
    pads = [p for ps in pcb.pads_by_net.values() for p in ps]
    xs = [p.global_x for p in pads]
    ys = [p.global_y for p in pads]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _circle(cx, cy, r, n):
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _counted(pcb, cfg):
    """Plan, counting segment distances and pad extent reads."""
    import escape_reserve as er
    import fine_escape as fe
    n = {'seg': 0, 'ext': 0}
    seg, ext = er._seg_seg, fe._half_extents

    def cseg(*a):
        n['seg'] += 1
        return seg(*a)

    def cext(*a):
        n['ext'] += 1
        return ext(*a)
    er._seg_seg, fe._half_extents = cseg, cext
    try:
        plan = rules._plan(pcb, cfg)
    finally:
        er._seg_seg, fe._half_extents = seg, ext
    return plan, n


def _with_outline(points):
    pcb = rules._load()
    cx, cy = _centre(pcb)
    pcb.board_info.board_outlines = [_circle(cx, cy, 40.0, points)]
    return pcb


def test_far_outline_edges_cost_nothing():
    cfg = qfn._config()
    plan0, n0 = _counted(_with_outline(200), cfg)
    plan1, n1 = _counted(_with_outline(2000), cfg)
    assert plan0 == plan1 and plan0[0], 'the outline is far from every site'
    assert n1['seg'] == n0['seg'], \
        f"segment distances grew with far outline edges: {n0['seg']} -> {n1['seg']}"


def test_far_pads_read_once():
    cfg = qfn._config()
    pcb = rules._load()
    plan0, n0 = _counted(pcb, cfg)
    pcb = rules._load()
    cx, cy = _centre(pcb)
    src = next(p for ps in pcb.pads_by_net.values() for p in ps)
    other = max(pcb.pads_by_net) + 1      # a net of their own, not planned
    far = []
    for i in range(1000):
        q = copy.copy(src)
        q.global_x = cx + 20.0 + (i % 40) * 0.5
        q.global_y = cy + 20.0 + (i // 40) * 0.5
        q.net_id, q.net_name = other, "FAR"
        q.component_ref, q.pad_number = "FAR1", str(i + 1)
        far.append(q)
    pcb.pads_by_net[other] = far
    plan1, n1 = _counted(pcb, cfg)
    assert plan0 == plan1 and plan0[0], 'the added pads are far from every site'
    grew = n1['ext'] - n0['ext']
    assert grew <= 2 * len(far), \
        f'{len(far)} far pads cost {grew} extent reads (once per test, not per plan)'


if __name__ == '__main__':
    test_far_outline_edges_cost_nothing()
    test_far_pads_read_once()
    print('escape reserve scale: PASS')
