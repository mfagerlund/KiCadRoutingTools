"""_foreign_seg_arrays_splice patches the cached foreign-segment snapshot to exactly the rows, row ids and per-layer
arrays a fresh rebuild gives: random removals and additions, and the cases it must drop instead (vias changed, graphic
copper added)."""
import math
import os
import random
import sys
from types import SimpleNamespace as NS

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

import single_ended_routing as ser  # noqa: E402

LAYERS = ('F.Cu', 'In1.Cu', 'B.Cu')


def _seg(rnd):
    x, y = rnd.uniform(0, 10), rnd.uniform(0, 10)
    a = rnd.choice([0, 45, 90]) * math.pi / 180
    ln = rnd.uniform(0, 3)
    return NS(start_x=x, start_y=y, end_x=x + ln * math.cos(a), end_y=y + ln * math.sin(a),
              layer=rnd.choice(LAYERS), width=rnd.choice([0.1, 0.2]), net_id=rnd.randint(1, 5))


def _arrays(pcb):
    return {lay: ser._foreign_seg_arrays(pcb, lay) for lay in LAYERS}


def _same(a, b):
    return all(x.dtype == y.dtype and x.shape == y.shape and (x == y).all() for x, y in zip(a, b))


def test_splice_equals_rebuild():
    rnd = random.Random(3)
    for trial in range(80):
        segs = [_seg(rnd) for _ in range(rnd.randint(0, 40))]
        vias = [NS(x=rnd.uniform(0, 10), y=rnd.uniform(0, 10), size=0.45, net_id=rnd.randint(1, 5))
                for _ in range(rnd.randint(0, 5))]
        pcb = NS(segments=segs, vias=vias, footprints=None)
        _arrays(pcb)
        removed = {id(s) for s in segs if rnd.random() < 0.2}
        added = [_seg(rnd) for _ in range(rnd.randint(0, 6))]
        pcb.segments = [s for s in segs if id(s) not in removed] + added
        ser._foreign_seg_arrays_splice(pcb, removed, added)
        cache = pcb._foreign_seg_arr_cache
        assert cache is not None, trial
        snap = cache[1]['_all']
        assert _same(snap[:7], ser._seg_rows(pcb.segments)), trial
        assert (cache[1]['_ids'] == np.asarray([id(s) for s in pcb.segments], dtype=np.int64)).all(), trial
        fresh = NS(segments=list(pcb.segments), vias=pcb.vias, footprints=None)
        got, want = _arrays(pcb), _arrays(fresh)
        assert all(_same(got[lay], want[lay]) for lay in LAYERS), trial


def test_splice_drops_what_it_cannot_patch():
    rnd = random.Random(4)
    segs = [_seg(rnd) for _ in range(10)]
    pcb = NS(segments=segs, vias=[], footprints=None)
    _arrays(pcb)
    pcb.vias = [NS(x=1.0, y=1.0, size=0.45, net_id=1)]          # vias changed since the snapshot
    ser._foreign_seg_arrays_splice(pcb, set(), [])
    assert pcb._foreign_seg_arr_cache is None
    pcb.vias = []
    _arrays(pcb)
    g = _seg(rnd)
    g.graphic = True                                           # graphic copper rows are positional
    pcb.segments = segs + [g]
    ser._foreign_seg_arrays_splice(pcb, set(), [g])
    assert pcb._foreign_seg_arr_cache is None


if __name__ == '__main__':
    test_splice_equals_rebuild()
    test_splice_drops_what_it_cannot_patch()
    print('PASS')
