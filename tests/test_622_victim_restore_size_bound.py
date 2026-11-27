"""The #622 victim-priority restore never rips a squatter bigger than the victim.

The endgame restore rips the nets holding a stub victim's channel and
requeues them. A requeued squatter that comes back partial loses up to
(its pads - 1) connections; the restore gains at most (victim pads - 1).
Measured on one card: a 2-pad victim's restore ripped a 19-pad power tree
routed as signal copper, which came back with 17 of its connections open,
so the board shipped 12 more opens than without the restore.

Rule: a squatter with more pads than the victim is never ripped for it.
Equal or smaller squatters are still ripped, so the motivating class
(victims held by their own small rippers) is unchanged.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

FAILS = []


def check(name, cond, detail=''):
    print(f"--- {name}")
    if cond:
        print(f"  PASS{': ' + detail if detail else ''}")
    else:
        print(f"  FAIL: {detail}")
        FAILS.append(name)


def _board(sizes):
    return SimpleNamespace(pads_by_net={nid: [object()] * n
                                        for nid, n in sizes.items()})


def t_bigger_squatter_refused():
    from rip_restore import outweighing_squatters
    pcb = _board({1: 2, 2: 2, 3: 19})
    got = outweighing_squatters(pcb, 1, {2, 3})
    check('t_bigger_squatter_refused', got == [3], f"got {got}")


def t_equal_and_smaller_allowed():
    from rip_restore import outweighing_squatters
    pcb = _board({1: 4, 2: 4, 3: 2})
    got = outweighing_squatters(pcb, 1, {2, 3})
    check('t_equal_and_smaller_allowed', got == [], f"got {got}")


def t_unknown_victim_refuses_any_padded_squatter():
    from rip_restore import outweighing_squatters
    pcb = _board({2: 2})
    got = outweighing_squatters(pcb, 9, {2})
    check('t_unknown_victim_refuses_any_padded_squatter', got == [2],
          f"got {got}")


def t_sweep_consults_the_bound():
    path = os.path.join(os.path.dirname(__file__), '..', 'py_router',
                        'reroute_loop.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    i = src.find('def _victim_priority_sweep622')
    j = src.find('state.victim_authority_used.add(_vid)', i)
    body = src[i:j]
    check('t_sweep_consults_the_bound',
          i >= 0 and j > i and 'outweighing_squatters(' in body,
          'the sweep must call outweighing_squatters before claiming '
          'victim authority')


if __name__ == '__main__':
    t_bigger_squatter_refused()
    t_equal_and_smaller_allowed()
    t_unknown_victim_refuses_any_padded_squatter()
    t_sweep_consults_the_bound()
    if FAILS:
        print(f"FAILED: {FAILS}")
        sys.exit(1)
    print("ALL PASS")
