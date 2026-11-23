#!/usr/bin/env python3
"""A bare-ball rescue escape takes the net's width on each layer it uses.

The rescue's bare-ball escape (net_rescue, #666 family) asks generate_bga_fanout
for a dogbone at the run's one track width and shipped every escape track at
it. Under a layer-scoped width rule (outer 0.30 mm, inner 0.15 mm, min = max)
an escape on an inner layer came out 0.30 mm wide: a track_width violation.

net_rescue.escape_track_width maps each fanout track to the net's own width on
its layer, narrowing only (a narrower track keeps every clearance the fanout
checked at the wider width).

Checks:
  1. An inner-layer escape track of a layer-scoped net narrows to the inner width.
  2. Its outer-layer track keeps the outer width.
  3. A net with no per-layer rule keeps the fanout width.
  4. A rule WIDER than the fanout width does not widen the track.

    python3 tests/test_NNNN_rescue_escape_layer_width.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from routing_config import GridRouteConfig                      # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main():
    try:
        from net_rescue import escape_track_width
    except ImportError as e:
        check("net_rescue maps escape tracks to the layer's width", False, str(e))
        return
    cfg = GridRouteConfig(layers=['F.Cu', 'In1.Cu', 'B.Cu'], clearance=0.2,
                          track_width=0.3, via_size=0.5, via_drill=0.3)
    cfg.net_layer_widths = {1: {'F.Cu': 0.3, 'B.Cu': 0.3, 'In1.Cu': 0.15},
                            3: {'F.Cu': 0.4, 'In1.Cu': 0.4, 'B.Cu': 0.4}}
    w = escape_track_width(cfg, 1, 'In1.Cu', 0.3)
    check("inner escape narrows to the inner rule width", abs(w - 0.15) < 1e-9, f"{w}")
    w = escape_track_width(cfg, 1, 'F.Cu', 0.3)
    check("outer escape keeps the outer width", abs(w - 0.3) < 1e-9, f"{w}")
    w = escape_track_width(cfg, 2, 'In1.Cu', 0.3)
    check("a net without a layer rule keeps the fanout width", abs(w - 0.3) < 1e-9, f"{w}")
    w = escape_track_width(cfg, 3, 'In1.Cu', 0.3)
    check("a wider rule never widens the escape", abs(w - 0.3) < 1e-9, f"{w}")


if __name__ == '__main__':
    main()
    if failures:
        print(f"FAILED: {len(failures)}")
        sys.exit(1)
    print("PASSED")
