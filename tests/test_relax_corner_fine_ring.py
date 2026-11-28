#!/usr/bin/env python3
"""KICAD_RELAX_CORNER: exact corner ring on fine-pitch pads only.

The exact ring (ring_cell_blocked) frees the escape cells a uniform ring
costs around narrow fine-pitch pads; on wide pads it is not used, so their
keep-out is the uniform ring exactly as before. KICAD_RELAX_CORNER_FINE sets
the short-side limit; 0 gives every pad the exact ring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

import env_knobs  # noqa: E402
from routing_utils import exact_ring_pad, iter_pad_blocked_cells, \
    pad_blocked_cells_array  # noqa: E402

GRID = 0.05
MARGIN = 0.2


def _cells(hw, hh, monkeypatch, env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    env_knobs.refresh()
    a = {tuple(c) for c in pad_blocked_cells_array(0, 0, hw, hh, MARGIN, GRID)}
    b = set(iter_pad_blocked_cells(0, 0, hw, hh, MARGIN, GRID))
    assert a == b
    return a


def _uniform(hw, hh, monkeypatch):
    return _cells(hw, hh, monkeypatch, {'KICAD_RELAX_CORNER': '1',
                                        'KICAD_RELAX_CORNER_FINE': '0.0001'})


def test_short_side_decides(monkeypatch):
    monkeypatch.setenv('KICAD_RELAX_CORNER_FINE', '0.4')
    env_knobs.refresh()
    assert exact_ring_pad(0.15, 0.7)        # 0.3 x 1.4 fine-pitch pad
    assert not exact_ring_pad(0.3, 0.7)     # 0.6 wide
    monkeypatch.setenv('KICAD_RELAX_CORNER_FINE', '0')
    env_knobs.refresh()
    assert exact_ring_pad(1.8, 4.8)


def test_fine_pad_gets_exact_ring(monkeypatch):
    fine = _cells(0.15, 0.7, monkeypatch, {'KICAD_RELAX_CORNER': '1',
                                           'KICAD_RELAX_CORNER_FINE': None})
    assert fine < _uniform(0.15, 0.7, monkeypatch)


def test_wide_pad_keeps_uniform_ring(monkeypatch):
    wide = _cells(0.5, 0.7, monkeypatch, {'KICAD_RELAX_CORNER': '1',
                                          'KICAD_RELAX_CORNER_FINE': None})
    assert wide == _uniform(0.5, 0.7, monkeypatch)
    every = _cells(0.5, 0.7, monkeypatch, {'KICAD_RELAX_CORNER': '1',
                                           'KICAD_RELAX_CORNER_FINE': '0'})
    assert every < wide
    monkeypatch.delenv('KICAD_RELAX_CORNER')
    monkeypatch.delenv('KICAD_RELAX_CORNER_FINE')
    env_knobs.refresh()
