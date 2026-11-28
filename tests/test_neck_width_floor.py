#!/usr/bin/env python3
"""A necked track width is written on KiCad's 1 nm grid and never below the
floor it was necked to: a 0.19812 mm (7.8 mil) Board Setup minimum rounded to
0.1 um shipped as 0.1981 and KiCad flagged every such segment."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from routing_utils import neck_round  # noqa: E402


def test_floor_not_rounded_below():
    assert neck_round(0.19812, 0.19812) == 0.19812
    assert neck_round(0.198119999, 0.19812) >= 0.19812


def test_width_kept_on_nm_grid():
    assert neck_round(0.1984961, 0.19812) == 0.198496
    assert neck_round(0.25, 0.2) == 0.25


def test_below_floor_lifted():
    assert neck_round(0.1981, 0.19812) == 0.19812
    assert neck_round(0.15, 0) == 0.15
