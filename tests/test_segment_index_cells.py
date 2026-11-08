"""SegmentIndex stamps the cells a segment's centreline crosses, not its bounding box (2026-11-08): every cell
the line touches is present, and query_near returns every segment within reach of a point, as the bounding-box
index did."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from check_connected import SegmentIndex  # noqa: E402
from geometry_utils import segment_to_segment_distance  # noqa: E402


class Seg:
    def __init__(self, x1, y1, x2, y2, layer='F.Cu', width=0.1):
        self.start_x, self.start_y, self.end_x, self.end_y = x1, y1, x2, y2
        self.layer, self.width = layer, width


def line_cells(seg, cs, n=4000):
    out = set()
    for i in range(n + 1):
        t = i / n
        x = seg.start_x + t * (seg.end_x - seg.start_x)
        y = seg.start_y + t * (seg.end_y - seg.start_y)
        out.add((int(x // cs), int(y // cs)))
    return out


def test_cells_cover_the_line_and_stay_in_the_bbox():
    rnd = random.Random(7)
    for _ in range(400):
        cs = rnd.choice([0.05, 0.1, 0.13, 1.0])
        x1, y1 = rnd.uniform(-5, 5), rnd.uniform(-5, 5)
        if rnd.random() < 0.5:
            d = rnd.uniform(-8, 8)
            x2, y2 = x1 + d, y1 + d * rnd.choice([1, -1])
        else:
            x2, y2 = rnd.uniform(-5, 5), rnd.uniform(-5, 5)
        seg = Seg(x1, y1, x2, y2)
        cells = SegmentIndex(cs)._cells_for_segment(seg)
        assert len(cells) == len(set(cells))
        got = set(cells)
        assert line_cells(seg, cs) <= got, (seg.__dict__, cs)
        gx1, gx2 = sorted((int(x1 // cs), int(x2 // cs)))
        gy1, gy2 = sorted((int(y1 // cs), int(y2 // cs)))
        assert all(gx1 <= gx <= gx2 and gy1 <= gy <= gy2 for gx, gy in got)
        if abs(x2 - x1) > 3 and abs(y2 - y1) > 3 and cs <= 0.1:
            assert len(got) < (gx2 - gx1 + 1) * (gy2 - gy1 + 1) / 5


def test_axis_aligned_and_grid_edge_segments():
    idx = SegmentIndex(0.1)
    assert idx._cells_for_segment(Seg(0.05, 0.05, 0.95, 0.05)) == [(gx, 0) for gx in range(10)]
    assert idx._cells_for_segment(Seg(0.05, 0.95, 0.05, 0.05)) == [(0, gy) for gy in range(10)]
    # a 45-degree line through cell corners keeps the cells on both sides of each corner
    cells = set(idx._cells_for_segment(Seg(0.0, 0.0, 1.0, 1.0)))
    assert line_cells(Seg(0.0, 0.0, 1.0, 1.0), 0.1) <= cells


def test_query_near_finds_every_segment_in_reach():
    rnd = random.Random(11)
    cs = 0.12
    segs = []
    idx = SegmentIndex(cs)
    for i in range(300):
        x1, y1 = rnd.uniform(0, 20), rnd.uniform(0, 20)
        ang = rnd.choice([0, 45, 90, 135]) * math.pi / 180 + (rnd.random() < 0.2) * rnd.uniform(0, 1)
        ln = rnd.uniform(0, 12)
        sg = Seg(x1, y1, x1 + ln * math.cos(ang), y1 + ln * math.sin(ang))
        segs.append(sg)
        idx.add(sg, 2 * i)
    for _ in range(3000):
        px, py = rnd.uniform(0, 20), rnd.uniform(0, 20)
        radius = rnd.choice([0.05, 0.11, 0.3])
        found = {id(s) for s, _ in idx.query_near(px, py, 'F.Cu', radius=radius)}
        for sg in segs:
            if segment_to_segment_distance(sg.start_x, sg.start_y, sg.end_x, sg.end_y, px, py, px, py) <= radius:
                assert id(sg) in found, (px, py, radius, sg.__dict__)


if __name__ == '__main__':
    test_cells_cover_the_line_and_stay_in_the_bbox()
    test_axis_aligned_and_grid_edge_segments()
    test_query_near_finds_every_segment_in_reach()
    print('PASS')
