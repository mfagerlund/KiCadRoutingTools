"""check_drc.any_point_within_polys answers exactly as the scalar loop it replaces.

The broadcast only nominates points; these cases put points on vertices, on edges, just
inside and just outside, at reach 0 and at a stroke's half width, above the size where the
broadcast runs."""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import check_drc  # noqa: E402


def _scalar(points, polys, reach):
    return any(check_drc._point_to_polys_distance(x, y, polys) <= reach + 1e-9 for (x, y) in points)


def _star(rng, cx, cy, n):
    import math
    pts = []
    for k in range(n):
        a = 2 * math.pi * k / n
        r = rng.uniform(0.3, 1.5)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def test_matches_scalar_on_random_cases():
    rng = random.Random(1181)
    checked = 0
    for case in range(300):
        polys = [_star(rng, rng.uniform(-1, 1), rng.uniform(-1, 1), rng.randint(40, 160))
                 for _ in range(rng.randint(1, 3))]
        verts = [p for poly in polys for p in poly]
        points = []
        for _ in range(rng.randint(60, 400)):
            kind = rng.random()
            if kind < 0.2:
                points.append(rng.choice(verts))
            elif kind < 0.4:
                poly = rng.choice(polys)
                i = rng.randrange(len(poly))
                (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
                t = rng.random()
                e = rng.choice((0.0, 1e-10, -1e-10, 1e-7, -1e-7))
                points.append((x1 + t * (x2 - x1) + e, y1 + t * (y2 - y1) - e))
            else:
                points.append((rng.uniform(-4, 4) + 6 * (case % 2), rng.uniform(-4, 4)))
        for reach in (0.0, 0.075, 0.2):
            assert check_drc.any_point_within_polys(points, polys, reach) == _scalar(points, polys, reach), \
                (case, reach)
            checked += 1
            # one point at a time decides each nominee alone
            for p in points[:20]:
                assert check_drc.any_point_within_polys([p] * 64, polys, reach) == _scalar([p], polys, reach)
    assert checked == 900


def test_far_points_are_false_and_inside_points_true():
    square = [[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)] * 1]
    ring = [(0.0 + 2.0 * k / 200, 0.0) for k in range(200)] + [(2.0, 2.0), (0.0, 2.0)]
    far = [(10.0 + i * 0.01, 10.0) for i in range(5000)]
    assert check_drc.any_point_within_polys(far, square, 0.1) is False
    assert check_drc.any_point_within_polys(far, [ring], 0.1) is False
    assert check_drc.any_point_within_polys(far + [(1.0, 1.0)], [ring], 0.0) is True
    assert check_drc.any_point_within_polys(far + [(1.0, 2.05)], [ring], 0.05) is True
    assert check_drc.any_point_within_polys(far + [(1.0, 2.06)], [ring], 0.05) is False
