#!/usr/bin/env python3
"""A stub layer switch takes the new layer's width under a layer-scoped width rule.

A `.kicad_dru` rule scoped by layer (`(layer outer)` 0.30 mm, `(layer inner)`
0.15 mm, min = max) gives a net one width per layer. apply_stub_layer_switch
moved a stub to the other layer and kept the old layer's width, so an outer
stub switched inward shipped at 0.30 mm on the inner layer (and the reverse),
a track_width violation on every moved segment.

Checks:
  1. A stub drawn at the old layer's rule width takes the new layer's width
     in memory, and the segment_mods record carries it.
  2. modify_segment_layers writes the new width into the file with the layer.
  3. revert_stub_layer_switch restores layer AND width.
  4. A stub at a width of its own (not the old layer's rule width) keeps it.
  5. A run-wide --impedance width map (config.layer_widths, no per-net rule)
     does not re-width: a diff-pair member's width is tied to its gap, and
     widening it in place ran it into its partner (test_diff_pair_route).

    python3 tests/test_NNNN_stub_switch_layer_width.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from synth import make_net, make_pad, make_pcb, make_seg        # noqa: E402
from kicad_parser import BoardInfo                              # noqa: E402
from routing_config import GridRouteConfig                      # noqa: E402
from kicad_writer import modify_segment_layers                  # noqa: E402
import stub_layer_switching as sls                              # noqa: E402

failures = []
OUTER_W, INNER_W = 0.3, 0.15


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def board(width):
    bi = BoardInfo(layers={0: 'F.Cu', 4: 'In1.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'In1.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 40.0, 30.0))
    stub = make_seg(10, 15, 14, 15, net_id=1, width=width, layer='In1.Cu')
    pads = [make_pad(1, 10, 15, ref='P1', size_x=0.8, size_y=0.8,
                     layers=['F.Cu', 'In1.Cu', 'B.Cu']),
            make_pad(1, 30, 15, ref='P2', size_x=0.8, size_y=0.8,
                     layers=['F.Cu', 'In1.Cu', 'B.Cu'])]
    return make_pcb(nets={1: make_net(1, 'NET_A')}, segments=[stub],
                    pads_by_net={1: pads}, board_info=bi), stub


def config():
    cfg = GridRouteConfig(layers=['F.Cu', 'In1.Cu', 'B.Cu'], clearance=0.2,
                          track_width=OUTER_W, via_size=0.5, via_drill=0.3)
    cfg.net_layer_widths = {1: {'F.Cu': OUTER_W, 'B.Cu': OUTER_W,
                                'In1.Cu': INNER_W}}
    return cfg


def file_text(seg, width):
    return (f'(kicad_pcb\n\t(segment\n\t\t(start {seg.start_x:.6f} {seg.start_y:.6f})\n'
            f'\t\t(end {seg.end_x:.6f} {seg.end_y:.6f})\n\t\t(width {width})\n'
            f'\t\t(layer "In1.Cu")\n\t\t(net "NET_A")\n\t\t(uuid "u1")\n\t)\n)\n')


def main():
    print("1-3. a rule-width stub switched In1.Cu -> F.Cu")
    cfg = config()
    pcb, stub = board(INNER_W)
    info = sls.get_stub_info(pcb, 1, 14, 15, 'In1.Cu')
    check("precondition: the stub is a switch candidate", info is not None)
    if info is None:
        return
    vias, mods = sls.apply_stub_layer_switch(pcb, info, 'F.Cu', cfg, debug=False)
    check("moved to F.Cu", stub.layer == 'F.Cu', stub.layer)
    check("takes the outer rule width", abs(stub.width - OUTER_W) < 1e-9,
          f"{stub.width}")
    out, _ = modify_segment_layers(file_text(stub, INNER_W), mods)
    check("file carries the new layer", '(layer "F.Cu")' in out)
    check("file carries the new width", f'(width {OUTER_W})' in out, out.split('(width')[1][:12])
    sls.revert_stub_layer_switch(pcb, mods, vias)
    check("revert restores the layer", stub.layer == 'In1.Cu', stub.layer)
    check("revert restores the width", abs(stub.width - INNER_W) < 1e-9, f"{stub.width}")

    print("4. a stub at a width of its own keeps it")
    pcb, stub = board(0.2)
    info = sls.get_stub_info(pcb, 1, 14, 15, 'In1.Cu')
    sls.apply_stub_layer_switch(pcb, info, 'F.Cu', config(), debug=False)
    check("own width kept", abs(stub.width - 0.2) < 1e-9, f"{stub.width}")

    print("5. a run-wide impedance width map does not re-width")
    pcb, stub = board(INNER_W)
    cfg = GridRouteConfig(layers=['F.Cu', 'In1.Cu', 'B.Cu'], clearance=0.2,
                          track_width=OUTER_W, via_size=0.5, via_drill=0.3)
    cfg.layer_widths = {'F.Cu': OUTER_W, 'B.Cu': OUTER_W, 'In1.Cu': INNER_W}
    info = sls.get_stub_info(pcb, 1, 14, 15, 'In1.Cu')
    sls.apply_stub_layer_switch(pcb, info, 'F.Cu', cfg, debug=False)
    check("impedance-map stub keeps its width", abs(stub.width - INNER_W) < 1e-9,
          f"{stub.width}")


if __name__ == '__main__':
    main()
    if failures:
        print(f"FAILED: {len(failures)}")
        sys.exit(1)
    print("PASSED")
