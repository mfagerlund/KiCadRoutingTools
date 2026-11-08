"""Via rungs keep the board's copper-to-hole floor (--hole-clearance / KICAD_HOLE_CLEARANCE)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
import fab_tiers as ft  # noqa: E402


def _ladder(hole, policy='fab'):
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    try:
        ft.set_default_fab_tier('auto', {})
        ft.set_escalation_policy(policy, {})
        ft.set_hole_clearance(hole)
        return ft.escalation_rungs(4)
    finally:
        ft.set_hole_clearance(None)
        ft.set_default_fab_tier(*prev_tier)
        ft.set_escalation_policy(*prev_pol)


def test_rungs_raised_to_hole_clearance():
    plain = _ladder(None)
    assert any((r['via_diameter'] - r['via_drill']) / 2 + r['clearance'] < 0.25 - 1e-9 for r in plain), plain
    for r in _ladder(0.25):
        assert (r['via_diameter'] - r['via_drill']) / 2 + r['clearance'] >= 0.25 - 1e-9, r


def test_env_default(monkeypatch):
    monkeypatch.setenv('KICAD_HOLE_CLEARANCE', '0.25')
    assert ft._hole_clearance_default() == 0.25
    monkeypatch.setenv('KICAD_HOLE_CLEARANCE', 'x')
    assert ft._hole_clearance_default() is None
    monkeypatch.delenv('KICAD_HOLE_CLEARANCE')
    assert ft._hole_clearance_default() is None
