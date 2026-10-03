#!/usr/bin/env python3
"""Locked routed copper is immutable across cleanup and writeback.

Run:
    python3 tests/test_locked_copper_contract.py
"""
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))

from kicad_parser import BoardInfo, Net, PCBData, Pad, Segment, Via, Zone  # noqa: E402
from kicad_writer import assert_locked_copper_preserved  # noqa: E402
from pcb_modification import nudge_grazing_microshift  # noqa: E402
from tap_relocation import find_relocation_candidate, relocate_tap  # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _pad(ref, x, y, net_id, net_name, sx=0.5, sy=0.5):
    return Pad(component_ref=ref, pad_number='1', global_x=x, global_y=y,
               local_x=0.0, local_y=0.0, size_x=sx, size_y=sy, shape='rect',
               layers=['F.Cu'], net_id=net_id, net_name=net_name)


def _board(segs, pads_by_net):
    return PCBData(
        board_info=BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                             copper_layers=['F.Cu', 'B.Cu'],
                             board_bounds=(-10.0, -10.0, 10.0, 10.0),
                             stackup=[]),
        nets={1: Net(1, 'LOCKED_NET'), 2: Net(2, 'OTHER')},
        footprints={}, vias=[], segments=list(segs), pads_by_net=pads_by_net)


def test_microshift_does_not_reemit_locked_segment():
    locked = Segment(start_x=0.0, start_y=0.0, end_x=2.0, end_y=0.0,
                     width=0.2, layer='F.Cu', net_id=1,
                     uuid='locked-seg', locked=True)
    pads = {
        1: [_pad('J1', 0.0, 0.0, 1, 'LOCKED_NET'),
            _pad('J2', 2.0, 0.0, 1, 'LOCKED_NET')],
        # Close enough to make the segment a graze; before the guard the
        # micro-shift pass bowed the locked segment into fresh unlocked copper.
        2: [_pad('U9', 1.0, 0.18, 2, 'OTHER', sx=0.1, sy=0.1)],
    }
    pcb = _board([locked], pads)
    n, nets, strip, added = nudge_grazing_microshift(
        [], pcb, {1}, clearance=0.1, max_shift=0.25)
    check('microshift leaves locked copper untouched',
          n == 0 and nets == 0 and strip == [] and added == []
          and pcb.segments == [locked] and locked.locked,
          f'n={n}, nets={nets}, strip={len(strip)}, added={len(added)}')


def _write(path, locked=True, moved=False):
    lock = ' (locked yes)' if locked else ''
    end = '(end 3 0)' if moved else '(end 2 0)'
    txt = f'''(kicad_pcb (version 20241229) (generator "pcbnew")
  (net 0 "")
  (net 1 "LOCKED_NET")
  (segment (start 0 0) {end} (width 0.2){lock} (layer "F.Cu") (net 1) (uuid "locked-seg"))
)
'''
    open(path, 'w', encoding='utf-8').write(txt)


def test_writer_guard_rejects_changed_locked_segment():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'in.kicad_pcb')
        out = os.path.join(d, 'out.kicad_pcb')
        _write(src, locked=True, moved=False)
        _write(out, locked=False, moved=False)
        try:
            assert_locked_copper_preserved(src, out)
        except RuntimeError as e:
            check('writer guard rejects lost lock flag', 'changed locked' in str(e)
                  or 'missing locked' in str(e) or 'missing' in str(e), str(e))
        else:
            check('writer guard rejects lost lock flag', False, 'no exception')

        _write(out, locked=True, moved=True)
        try:
            assert_locked_copper_preserved(src, out)
        except RuntimeError as e:
            check('writer guard rejects moved locked segment', 'changed' in str(e), str(e))
        else:
            check('writer guard rejects moved locked segment', False, 'no exception')


def test_tap_relocation_refuses_locked_tap():
    via = Via(x=1.0, y=0.0, size=0.5, drill=0.25,
              layers=['F.Cu', 'B.Cu'], net_id=1,
              uuid='locked-via', locked=True)
    stub = Segment(start_x=1.0, start_y=0.0, end_x=1.5, end_y=0.0,
                   width=0.2, layer='F.Cu', net_id=1,
                   uuid='locked-stub', locked=True)
    pcb = _board([stub], {1: [_pad('J1', 1.5, 0.0, 1, 'LOCKED_NET')]})
    pcb.vias = [via]
    pcb.zones = [Zone(net_id=1, net_name='LOCKED_NET', layer='F.Cu',
                      polygon=[(0, -1), (2, -1), (2, 1), (0, 1)])]

    cand = find_relocation_candidate(pcb, None, {1: {}}, (1.0, 0.0),
                                     blamed_net_ids={1}, radius_mm=3.0)
    check('tap relocation does not select locked tap copper', cand is None)

    moved = relocate_tap(pcb, None, {}, {1: {}}, via)
    check('tap relocation refuses direct locked via removal',
          moved is None and pcb.vias == [via] and pcb.segments == [stub])


def main():
    test_microshift_does_not_reemit_locked_segment()
    test_writer_guard_rejects_changed_locked_segment()
    test_tap_relocation_refuses_locked_tap()
    if FAILS:
        print(f"\nFAIL ({len(FAILS)}): " + '; '.join(FAILS))
        return 1
    print("\nPASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
