"""FragilityField.publish merges per layer (2026-11-08) and re-merges only the layers a refresh dirtied: the
published rows equal the old whole-board merge (one sort over every pour's rows, max cost per cell), after a full
publish and after partial ones."""
import os
import random
import sys
from types import SimpleNamespace as NS

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from plane_fragility import FragilityField, PLANE_FRAGILITY_CACHE_KEY  # noqa: E402


def whole_board(states):
    rows = [st.rows for st in states if st.rows is not None]
    if not rows:
        return None
    out = np.vstack(rows)
    order = np.lexsort((out[:, 3], out[:, 2], out[:, 1], out[:, 0]))
    out = out[order]
    same = (np.diff(out[:, 0]) == 0) & (np.diff(out[:, 1]) == 0) & (np.diff(out[:, 2]) == 0)
    keep = np.ones(len(out), dtype=bool)
    keep[:-1][same] = False
    return out[keep]


def rand_rows(rnd, li):
    n = rnd.randint(0, 300)
    if n == 0:
        return None
    return np.column_stack([np.full(n, li, dtype=np.int32),
                            np.array([rnd.randint(0, 40) for _ in range(n)], dtype=np.int32),
                            np.array([rnd.randint(0, 40) for _ in range(n)], dtype=np.int32),
                            np.array([rnd.randint(1, 9) for _ in range(n)], dtype=np.int32)])


def test_publish_matches_whole_board_merge():
    rnd = random.Random(2)
    for _ in range(40):
        states = [NS(li=rnd.randint(0, 3), rows=None) for _ in range(rnd.randint(1, 7))]
        for st in states:
            st.rows = rand_rows(rnd, st.li)
        fld = FragilityField.__new__(FragilityField)
        fld.states, fld.cache = states, {}
        fld.publish()
        for step in range(6):
            want = whole_board(states)
            got = fld.cache.get(PLANE_FRAGILITY_CACHE_KEY)
            if want is None:
                assert got is None
            else:
                assert got is not None and np.array_equal(got, want), step
            dirty = set()
            for st in rnd.sample(states, rnd.randint(1, len(states))):
                st.rows = rand_rows(rnd, st.li)
                dirty.add(st.li)
            fld.publish(dirty)


if __name__ == '__main__':
    test_publish_matches_whole_board_merge()
    print('PASS')
