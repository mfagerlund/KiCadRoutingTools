#!/usr/bin/env python3
"""Fine-pitch connectors get pseudo-stub escape reservation.

`get_chip_pad_positions` reserves the pad exits of fine-pitch packages so that
other nets pay to route across them. The package gate admits BGA/QFN/QFP only,
so a 0.4-0.5mm board-to-board, FFC or module connector (package 'OTHER') had
no reservation: other nets route across the only exits its pad row has, and
the connector's own nets then fail at the pad. watchy's J3 is a 26-pad 0.5mm
FPC connector; on main none of its pads is a pseudo-stub.

The pitch admission must not reach coarse parts: 2.54mm headers were the
regressions that put dense connectors out of the gate (#585 item 8), and small
SOT parts at 0.5-0.65mm escape on their own.

    python3 tests/test_connector_pseudo_stubs.py
"""
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_R, os.path.join(_R, 'py_router'), os.path.join(_R, 'py_tools')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import env_knobs                                                  # noqa: E402
from kicad_parser import parse_kicad_pcb                          # noqa: E402
from net_queries import (footprint_min_pad_pitch,                 # noqa: E402
                         get_all_unrouted_net_ids,
                         get_chip_pad_positions)

CHECKS = []


def check(name, ok, detail=''):
    CHECKS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}" + (f' -- {detail}' if not ok else ''))


def _stub_pads(pcb, ref):
    """(x, y) of `ref`'s pads among the pseudo-stub positions."""
    fp = pcb.footprints[ref]
    pads = {(round(p.global_x, 4), round(p.global_y, 4)) for p in fp.pads}
    pcb._chip_refs_memo = {}
    got = get_chip_pad_positions(pcb, get_all_unrouted_net_ids(pcb))
    return {(round(s[0], 4), round(s[1], 4)) for s in got} & pads


def main():
    pcb = parse_kicad_pcb(os.path.join(_R, 'kicad_files', 'watchy.kicad_pcb'))
    j3 = pcb.footprints['J3']
    check('J3 is a 0.5mm connector', abs(footprint_min_pad_pitch(j3) - 0.5) < 0.01,
          f'pitch {footprint_min_pad_pitch(j3)}')

    os.environ.pop('KICAD_PSEUDO_STUB_MAX_PITCH', None)
    env_knobs.refresh()
    on = _stub_pads(pcb, 'J3')
    check('J3 pads are pseudo-stubs', len(on) >= 10, f'{len(on)} J3 pads emitted')

    coarse = [ref for ref, fp in pcb.footprints.items()
              if len(fp.pads) >= 10 and footprint_min_pad_pitch(fp) > 1.0]
    leaked = [ref for ref in coarse if _stub_pads(pcb, ref)]
    check('coarse-pitch parts stay out', not leaked, f'emitted: {leaked}')

    os.environ['KICAD_PSEUDO_STUB_MAX_PITCH'] = '0'
    env_knobs.refresh()
    try:
        off = _stub_pads(pcb, 'J3')
    finally:
        os.environ.pop('KICAD_PSEUDO_STUB_MAX_PITCH', None)
        env_knobs.refresh()
    check('knob 0 restores the package-only gate', not off, f'{len(off)} J3 pads emitted')

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
