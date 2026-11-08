"""The escape grid's cached foreign-track stamp (2026-11-08) is the grid block_segment builds directly: OR-ing it in
gives identical bytes on a fresh grid and on one that already carries other stamps, on a miss and on a hit."""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from bga_fanout.underpad import _Occ, _stamp_segments_memo, _SEG_STAMP_MEMO  # noqa: E402


def test_memo_equals_direct():
    rnd = random.Random(3)
    bounds, res, layers = (0.0, 0.0, 6.0, 5.0), 0.025, ['F.Cu', 'In2.Cu', 'B.Cu']
    rows = [(rnd.randrange(3), rnd.uniform(0, 6), rnd.uniform(0, 5), rnd.uniform(0, 6), rnd.uniform(0, 5),
             rnd.choice([0.165, 0.2, 0.3])) for _ in range(40)]
    _SEG_STAMP_MEMO.clear()
    for _ in range(2):                                   # miss, then hit
        for pre in (False, True):
            direct, memo = _Occ(bounds, res, layers), _Occ(bounds, res, layers)
            if pre:
                for o in (direct, memo):
                    o.block_all(3.0, 2.5, 0.4)
            for li, ax, ay, bx, by, r in rows:
                direct.block_segment(li, (ax, ay), (bx, by), r)
            _stamp_segments_memo(memo, rows)
            assert [bytes(g) for g in memo.grid] == [bytes(g) for g in direct.grid]
    assert len(_SEG_STAMP_MEMO) == 1


if __name__ == '__main__':
    test_memo_equals_direct()
    print('PASS')
