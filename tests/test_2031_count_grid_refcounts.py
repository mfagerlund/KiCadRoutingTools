#!/usr/bin/env python3
"""Obstacle-map refcounts held in a tiled dense grid behave exactly like the
hash maps they replace.

A random sequence of cell / via / via-rung stamps and removals (single and
batch, column spans, negative coordinates, tile borders) is applied to a
GridObstacleMap and to a plain Python dict model. After every phase:
  1. is_blocked / is_via_blocked agree with "refcount > 0" on every touched
     cell and a ring around it;
  2. get_stats counts (blocked cells, vias, small-rung vias) match the model;
  3. blocked_via_cells_at_rung exports the model's cell set, sorted;
  4. clone_fresh and merge_blocked_from preserve and add counts;
  5. removing everything that was added leaves an empty map.
"""
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'rust_router'))
sys.path.insert(0, os.path.join(HERE, '..'))

import grid_router  # noqa: E402

LAYERS = 3


class Model:
    def __init__(self):
        self.cells = [dict() for _ in range(LAYERS)]
        self.rungs = [dict(), dict()]  # rung 0 = vias, rung 1 = small vias

    @staticmethod
    def _inc(d, k, n=1):
        d[k] = (d.get(k, 0) + n) & 0xFFFF
        if d[k] == 0:
            del d[k]

    @staticmethod
    def _dec(d, k):
        c = d.get(k, 0)
        if c > 1:
            d[k] = c - 1
        elif c == 1:
            del d[k]


def apply_ops(m, model, rng, n):
    touched = set()
    for _ in range(n):
        op = rng.randrange(8)
        gx = rng.randrange(-140, 140)
        gy = rng.randrange(-140, 140)
        layer = rng.randrange(LAYERS)
        touched.add((gx, gy))
        if op == 0:
            m.add_blocked_cell(gx, gy, layer)
            model._inc(model.cells[layer], (gx, gy))
        elif op == 1:
            m.remove_blocked_cells_batch(np.array([[gx, gy, layer]], dtype=np.int32))
            model._dec(model.cells[layer], (gx, gy))
        elif op == 2:
            lo, hi = gy, gy + rng.randrange(0, 90)
            m.add_blocked_cell_spans_batch(np.array([[gx, lo, hi, layer]], dtype=np.int32))
            for y in range(lo, hi + 1):
                model._inc(model.cells[layer], (gx, y))
                touched.add((gx, y))
        elif op == 3:
            lo, hi = gy, gy + rng.randrange(0, 90)
            m.remove_blocked_cell_spans_batch(np.array([[gx, lo, hi, layer]], dtype=np.int32))
            for y in range(lo, hi + 1):
                model._dec(model.cells[layer], (gx, y))
                touched.add((gx, y))
        elif op == 4:
            m.add_blocked_via(gx, gy)
            model._inc(model.rungs[0], (gx, gy))
        elif op == 5:
            m.remove_blocked_vias_batch(np.array([[gx, gy]], dtype=np.int32))
            model._dec(model.rungs[0], (gx, gy))
        elif op == 6:
            m.add_blocked_via_rung(1, gx, gy)
            model._inc(model.rungs[1], (gx, gy))
        else:
            m.remove_blocked_via_rung(1, gx, gy)
            model._dec(model.rungs[1], (gx, gy))
    return touched


def check(m, model, touched, tag):
    ring = set()
    for (x, y) in touched:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                ring.add((x + dx, y + dy))
    for (x, y) in ring:
        for layer in range(LAYERS):
            assert m.is_blocked(x, y, layer) == ((x, y) in model.cells[layer]), (tag, x, y, layer)
        assert m.is_via_blocked(x, y) == ((x, y) in model.rungs[0]), (tag, x, y)
    st = m.get_stats()
    assert st[0] == sum(len(d) for d in model.cells), (tag, st)
    assert st[1] == len(model.rungs[0]), (tag, st)
    assert st[7] == len(model.rungs[1]), (tag, st)
    assert list(m.blocked_via_cells_at_rung(0)) == sorted(model.rungs[0]), tag
    assert list(m.blocked_via_cells_at_rung(1)) == sorted(model.rungs[1]), tag


def test_count_grid_refcounts():
    rng = random.Random(2031)
    m = grid_router.GridObstacleMap(LAYERS)
    model = Model()
    touched = set()
    for phase in range(4):
        touched |= apply_ops(m, model, rng, 4000)
        check(m, model, touched, f'phase{phase}')

    c = m.clone_fresh()
    check(c, model, touched, 'clone')

    # merge: a second map's counts add onto the first's.
    other = grid_router.GridObstacleMap(LAYERS)
    om = Model()
    touched |= apply_ops(other, om, rng, 3000)
    for layer in range(LAYERS):
        for k, v in om.cells[layer].items():
            Model._inc(model.cells[layer], k, v)
    for k, v in om.rungs[0].items():
        Model._inc(model.rungs[0], k, v)
    m.merge_blocked_from(other)
    # merge carries cells and full-size vias, not other rungs.
    check(m, model, touched, 'merge')

    # remove everything: back to empty.
    for layer in range(LAYERS):
        rows = [[x, y, layer] for (x, y), cnt in model.cells[layer].items() for _ in range(cnt)]
        if rows:
            m.remove_blocked_cells_batch(np.array(rows, dtype=np.int32))
    rows = [[x, y] for (x, y), cnt in model.rungs[0].items() for _ in range(cnt)]
    if rows:
        m.remove_blocked_vias_batch(np.array(rows, dtype=np.int32))
    for (x, y), cnt in list(model.rungs[1].items()):
        for _ in range(cnt):
            m.remove_blocked_via_rung(1, x, y)
    empty = Model()
    check(m, empty, touched, 'emptied')


if __name__ == '__main__':
    test_count_grid_refcounts()
    print('PASS')
