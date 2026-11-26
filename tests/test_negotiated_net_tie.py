"""Negotiated router own-copper lifts on a declared net tie: a footprint's tie line lifted for both tied nets is taken
off the base map once, and each cell it covers is owned by the nearest tied pad's net (not MULTI, which sealed both
pads: stmp157's OLIMEX SJ_2_SMALL_12_TIED jumpers declared as net ties)."""
import os
import sys
from types import SimpleNamespace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_router', 'rust_router'):
    sys.path.insert(0, os.path.join(ROOT, _p))
from negotiated import _lift_removal, _tie_owned_lifts  # noqa: E402
from routing_config import GridCoord  # noqa: E402


def _pad(num, net, x):
    return SimpleNamespace(pad_number=num, net_id=net, global_x=x, global_y=0.0)


def _board(groups):
    fp = SimpleNamespace(net_tie_groups=groups, pads=[_pad('1', 10, 0.0), _pad('2', 11, 1.0), _pad('3', 12, 2.0)])
    return SimpleNamespace(footprints={'JP1': fp})


def main():
    coord = GridCoord(0.1)
    line = np.array([[gx, 0, 0] for gx in range(0, 11)], dtype=np.int32)  # pad 1 to pad 2 on layer 0
    lifts = {10: (line, np.zeros((0, 2), np.int32)), 11: (line.copy(), np.zeros((0, 2), np.int32))}
    fails = 0

    rem = _lift_removal(lifts, 0)
    ok = len(rem) == len(line) and {tuple(r) for r in rem.tolist()} == {tuple(r) for r in line.tolist()}
    print(('ok  ' if ok else 'FAIL') + f' a line lifted for both tied nets comes off the base once ({len(rem)} rows)')
    fails += not ok

    owned = _tie_owned_lifts(_board([['1', '2']]), lifts, coord)
    a = {r[0] for r in owned[10][0].tolist()}
    b = {r[0] for r in owned[11][0].tolist()}
    ok = a == set(range(0, 6)) and b == set(range(6, 11)) and not (a & b)
    print(('ok  ' if ok else 'FAIL') + f' declared tie: each cell owned by the nearest tied pad ({sorted(a)} / {sorted(b)})')
    fails += not ok

    same = _tie_owned_lifts(_board([]), lifts, coord)
    ok = len(same[10][0]) == len(line) and len(same[11][0]) == len(line)
    print(('ok  ' if ok else 'FAIL') + ' no declared tie: both nets keep the claim (MULTI, blocked for both)')
    fails += not ok

    other = {10: lifts[10], 12: (line.copy(), np.zeros((0, 2), np.int32))}
    kept = _tie_owned_lifts(_board([['1', '2']]), other, coord)
    ok = len(kept[10][0]) == len(line) and len(kept[12][0]) == len(line)
    print(('ok  ' if ok else 'FAIL') + ' nets outside the tie group keep the shared claim')
    fails += not ok

    print(f'\n{fails} failure(s)')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
