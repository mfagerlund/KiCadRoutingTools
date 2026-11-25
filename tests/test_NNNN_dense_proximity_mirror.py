#!/usr/bin/env python3
"""The A* proximity lookups read a dense mirror of the stub / layer proximity
maps and return exactly what the maps hold.

A random sequence of proximity writes (set_stub_proximity, the max-mode and
sum-mode batches with and without max zones, set_layer_proximity and its numpy
batch), clears and clones is applied to a GridObstacleMap and to a Python
model of the maps (float32 falloff, as in Rust). After every phase, on every
touched cell and a ring around it:
  1. get_stub_proximity_cost and is_in_any_proximity_zone agree with the model;
  2. get_layer_proximity_cost agrees per layer;
  3. get_cross_layer_attraction is suppressed exactly where the stub map has
     an entry;
  4. clone and clone_fresh carry the same values, and a write to the clone
     leaves the original alone.
Far outlier coordinates (beyond the mirror's area cap) are included, so the
spill fallback to the map is exercised too.
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
F1 = np.float32(1.0)


def falloff(dx, dy, radius, max_cost):
    dist = np.sqrt(np.float32(dx * dx + dy * dy))
    prox = F1 - dist / np.float32(radius)
    return int(prox * np.float32(max_cost))


def disk(radius):
    r2 = radius * radius
    return [(dx, dy) for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1) if dx * dx + dy * dy <= r2]


class Model:
    def __init__(self):
        self.stub = {}
        self.layer = [dict() for _ in range(LAYERS)]

    def copy(self):
        m = Model()
        m.stub = dict(self.stub)
        m.layer = [dict(d) for d in self.layer]
        return m


def rand_xy(rng):
    if rng.random() < 0.01:
        return (1_000_000_000 - rng.randrange(5), -1_000_000_000 + rng.randrange(5))
    return (rng.randrange(-300, 600), rng.randrange(-200, 400))


def apply_random(rng, om, m, touched, n):
    for _ in range(n):
        op = rng.randrange(6)
        if op == 0:
            x, y = rand_xy(rng)
            c = rng.randrange(1, 500)
            om.set_stub_proximity(x, y, c)
            if c > m.stub.get((x, y), 0):
                m.stub[(x, y)] = c
            touched.add((x, y))
        elif op == 1:
            pts = [rand_xy(rng) for _ in range(rng.randrange(1, 4))]
            r, mc = rng.randrange(1, 6), rng.randrange(1, 400)
            om.add_stub_proximity_costs_batch(pts, r, mc)
            for (cx, cy) in pts:
                for dx, dy in disk(r):
                    c = falloff(dx, dy, r, mc)
                    k = (cx + dx, cy + dy)
                    if c > m.stub.get(k, 0):
                        m.stub[k] = c
                    touched.add(k)
        elif op == 2:
            groups = []
            for _ in range(rng.randrange(1, 3)):
                pts = [rand_xy(rng) for _ in range(rng.randrange(1, 3))]
                groups.append((pts, rng.randrange(1, 6), rng.randrange(1, 400)))
            rects = None
            if rng.random() < 0.5:
                x0, y0 = rng.randrange(-300, 600), rng.randrange(-200, 400)
                rects = [(x0, y0, x0 + 40, y0 + 40)]
            om.add_stub_proximity_costs_grouped(groups, rects)
            for pts, r, mc in groups:
                field = {}
                for (cx, cy) in pts:
                    for dx, dy in disk(r):
                        c = falloff(dx, dy, r, mc)
                        if c > 0:
                            k = (cx + dx, cy + dy)
                            field[k] = max(field.get(k, 0), c)
                for k, c in field.items():
                    cur = m.stub.get(k, 0)
                    inz = rects and any(a <= k[0] <= c2 and b <= k[1] <= d
                                        for a, b, c2, d in rects)
                    m.stub[k] = max(cur, c) if inz else cur + c
                    touched.add(k)
        elif op == 3:
            x, y = rand_xy(rng)
            layer, c = rng.randrange(LAYERS), rng.randrange(-5, 300)
            om.set_layer_proximity(x, y, layer, c)
            if c > 0:
                m.layer[layer][(x, y)] = max(m.layer[layer].get((x, y), 0), c)
            touched.add((x, y))
        elif op == 4:
            rows = []
            for _ in range(rng.randrange(1, 40)):
                x, y = rand_xy(rng)
                layer, c = rng.randrange(LAYERS), rng.randrange(-5, 300)
                rows.append((layer, x, y, c))
                if c > 0:
                    m.layer[layer][(x, y)] = max(m.layer[layer].get((x, y), 0), c)
                touched.add((x, y))
            om.set_layer_proximity_batch(np.array(rows, dtype=np.int32))
        else:
            # a cross-layer track for the attraction check (layer 1 only)
            x, y = rand_xy(rng)
            om.add_cross_layer_track(x, y, 1)
            touched.add((x, y))


def check(om, m, touched, tracks):
    for (x, y) in touched:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (x + dx, y + dy)
                want = m.stub.get(k, 0)
                assert om.get_stub_proximity_cost(*k) == want, (k, want)
                assert om.is_in_any_proximity_zone(*k) == (want > 0), k
                for layer in range(LAYERS):
                    assert om.get_layer_proximity_cost(k[0], k[1], layer) == \
                        m.layer[layer].get(k, 0), (k, layer)
                if k in tracks:
                    a = om.get_cross_layer_attraction(k[0], k[1], 0, 1, 100)
                    assert (a == 0) == (want > 0), (k, a, want)


def test_dense_proximity_mirror():
    rng = random.Random(4242)
    om = grid_router.GridObstacleMap(LAYERS)
    m = Model()
    touched = set()
    for phase in range(6):
        apply_random(rng, om, m, touched, 300)
        tracks = set()
        # tracks were stamped through add_cross_layer_track in op 5; re-stamp a
        # known set so the check has cells to probe
        for k in list(touched)[:200]:
            om.add_cross_layer_track(k[0], k[1], 1)
            tracks.add(k)
        check(om, m, touched, tracks)

        c1, c2 = om.clone(), om.clone_fresh()
        check(c1, m, touched, tracks)
        check(c2, m, touched, tracks)
        # a write to a clone leaves the original alone
        x, y = next(iter(touched))
        c1.set_stub_proximity(x, y, 10_000)
        c1.set_layer_proximity(x, y, 2, 10_000)
        check(om, m, touched, tracks)

        if phase == 2:
            om.clear_stub_proximity()
            m.stub.clear()
            check(om, m, touched, tracks)
        if phase == 4:
            om.clear_layer_proximity()
            for d in m.layer:
                d.clear()
            check(om, m, touched, tracks)


if __name__ == '__main__':
    test_dense_proximity_mirror()
    print('ok')
