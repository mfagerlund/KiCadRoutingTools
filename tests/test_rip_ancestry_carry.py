#!/usr/bin/env python3
"""Rip ancestry must survive across the final-reconciliation laps.

Each reconciliation lap is a NESTED batch_route call with a fresh
RoutingState, so the cycle guard (record_rip_ancestry / rip_exclude_set) was
reset per lap. Lap 1: net A rips net B and lands. Lap 2: B (now the failing
net) sees A as its top blocker and rips A back -- the A<->B exchange repeats
every lap and ships the same open, even when a third blocker C would have
freed B (ftdi-jtag-programmer USB_D-/USB_D+ vs VBUS at the USB-C row).

The carry is keyed by net NAME (sub-runs reload the board), cleared at the
outermost run entry, and seeded into every new state as SOFT ancestry: the
earlier-lap ripper is ripped last (lap 2's D+ needs VBUS AND D- ripped), not
excluded.

Run:  python3 tests/test_rip_ancestry_carry.py [-v]
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))

from types import SimpleNamespace

import routing_state
from routing_state import (record_rip_ancestry, rip_exclude_set,
                           seed_carried_rip_ancestry, clear_carried_rip_ancestry,
                           soft_rip_ancestors, demote_soft_ancestors)


def _pcb(names):
    return SimpleNamespace(nets={i: SimpleNamespace(name=n)
                                 for i, n in names.items()})


def _state(pcb):
    return SimpleNamespace(rip_ancestry={}, carried_rip_ancestry={}, pcb_data=pcb)


def _b(nid, name):
    return SimpleNamespace(net_id=nid, net_name=name)


class CarryTest(unittest.TestCase):
    def setUp(self):
        clear_carried_rip_ancestry()

    def tearDown(self):
        clear_carried_rip_ancestry()

    def test_lap2_does_not_rip_back_lap1_ripper(self):
        pcb = _pcb({1: '/USB_D-', 2: '/USB_D+', 3: '/Power/VBUS'})
        lap1 = _state(pcb)
        record_rip_ancestry(lap1, 1, 2)          # D- ripped D+ in lap 1
        lap2 = _state(pcb)                       # fresh state for lap 2
        seed_carried_rip_ancestry(lap2)
        self.assertEqual(soft_rip_ancestors(lap2, 2), {1})
        self.assertEqual(rip_exclude_set(lap2, 2), {2})   # soft, not excluded
        order, demoted = demote_soft_ancestors(
            lap2, 2, [_b(1, '/USB_D-'), _b(3, '/Power/VBUS')])
        self.assertEqual([b.net_id for b in order], [3, 1])  # VBUS first
        self.assertEqual(demoted, ['/USB_D-'])

    def test_lone_ancestor_still_rippable(self):
        pcb = _pcb({1: 'A', 2: 'B'})
        lap1 = _state(pcb)
        record_rip_ancestry(lap1, 1, 2)
        lap2 = _state(pcb)
        seed_carried_rip_ancestry(lap2)
        order, demoted = demote_soft_ancestors(lap2, 2, [_b(1, 'A')])
        self.assertEqual([b.net_id for b in order], [1])
        self.assertEqual(demoted, [])

    def test_carry_maps_by_name_when_ids_change(self):
        lap1 = _state(_pcb({1: 'A', 2: 'B'}))
        record_rip_ancestry(lap1, 1, 2)
        lap2 = _state(_pcb({7: 'B', 9: 'A'}))    # reloaded board, new ids
        seed_carried_rip_ancestry(lap2)
        self.assertEqual(soft_rip_ancestors(lap2, 7), {9})

    def test_clear_at_run_entry(self):
        lap1 = _state(_pcb({1: 'A', 2: 'B'}))
        record_rip_ancestry(lap1, 1, 2)
        clear_carried_rip_ancestry()
        lap2 = _state(_pcb({1: 'A', 2: 'B'}))
        seed_carried_rip_ancestry(lap2)
        self.assertEqual(soft_rip_ancestors(lap2, 2), set())

    def test_in_lap_ancestry_stays_hard(self):
        pcb = _pcb({1: 'A', 2: 'B', 3: 'C'})
        lap1 = _state(pcb)
        record_rip_ancestry(lap1, 1, 2)
        lap2 = _state(pcb)
        seed_carried_rip_ancestry(lap2)
        record_rip_ancestry(lap2, 3, 2)          # C rips B in lap 2
        self.assertEqual(rip_exclude_set(lap2, 2) - {2}, {3})   # hard
        self.assertEqual(soft_rip_ancestors(lap2, 2), {1})      # soft

    def test_stub_state_without_pcb_is_noop(self):
        st = SimpleNamespace(rip_ancestry={})
        record_rip_ancestry(st, 1, 2)            # existing unit-test stubs
        seed_carried_rip_ancestry(st)
        self.assertEqual(st.rip_ancestry, {2: frozenset({1})})
        self.assertEqual(soft_rip_ancestors(st, 2), set())


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a == '-v'])
