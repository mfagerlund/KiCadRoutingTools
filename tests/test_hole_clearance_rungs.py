"""Via rungs keep the board's copper-to-hole floor (Board Setup min_hole_clearance)."""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

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


def _ring_to_hole(r):
    return (r['via_diameter'] - r['via_drill']) / 2 + r['clearance']


def test_rungs_raised_to_hole_clearance():
    plain = _ladder(None)
    assert any(_ring_to_hole(r) < 0.25 - 1e-9 for r in plain), plain
    raised = _ladder(0.25)
    assert raised and all(_ring_to_hole(r) >= 0.25 - 1e-9 for r in raised), raised
    # only the via diameter moves; drills and clearances stay the ladder's
    assert {(r['via_drill'], r['clearance']) for r in raised} <= {(r['via_drill'], r['clearance']) for r in plain}


def _policy_hole(pro_rules, policy='board'):
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    with tempfile.TemporaryDirectory() as d:
        pcb = os.path.join(d, 'b.kicad_pcb')
        open(pcb, 'w').write('(kicad_pcb)')
        if pro_rules is not None:
            with open(os.path.join(d, 'b.kicad_pro'), 'w') as f:
                json.dump({'board': {'design_settings': {'rules': pro_rules}}}, f)
        args = SimpleNamespace(escalation=policy, no_fix_drc_settings=True, clearance=0.2)
        try:
            ft.set_policy_from_args(args, pcb)
            return ft.get_hole_clearance()
        finally:
            ft.set_hole_clearance(None)
            ft.set_default_fab_tier(*prev_tier)
            ft.set_escalation_policy(*prev_pol)


def test_policy_reads_the_board_rule():
    assert _policy_hole({'min_hole_clearance': 0.19}) == 0.19
    assert _policy_hole({'min_clearance': 0.1}) == ft.KICAD_FACTORY_HOLE_CLEARANCE
    assert _policy_hole({'min_hole_clearance': 0.3}, policy='fab') == 0.3
    assert _policy_hole({'min_hole_clearance': 0.3}, policy='off') is None


def _board_rungs(pro_rules, policy='fab'):
    """The via rungs route.py escalates through on a board whose .kicad_pro
    carries `pro_rules`, set up the way route.py does (set_policy_from_args)."""
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    with tempfile.TemporaryDirectory() as d:
        pcb = os.path.join(d, 'b.kicad_pcb')
        open(pcb, 'w').write('(kicad_pcb)')
        with open(os.path.join(d, 'b.kicad_pro'), 'w') as f:
            json.dump({'board': {'design_settings': {'rules': pro_rules}}}, f)
        args = SimpleNamespace(escalation=policy, no_fix_drc_settings=True, clearance=0.2)
        try:
            ft.set_policy_from_args(args, pcb)
            return ft.escalation_rungs(4)
        finally:
            if hasattr(ft, 'set_hole_clearance'):
                ft.set_hole_clearance(None)
            ft.set_default_fab_tier(*prev_tier)
            ft.set_escalation_policy(*prev_pol)


def test_board_rule_binds_escalated_vias():
    """A board declaring min_hole_clearance 0.25 mm: every via rung keeps a
    neighbour at the rung's clearance >= 0.25 mm from the drill wall."""
    plain = _board_rungs({'min_hole_clearance': 0.0})
    assert any(_ring_to_hole(r) < 0.25 - 1e-9 for r in plain), (
        'non-vacuity: the unconstrained ladder has no rung under 0.25 mm', plain)
    rungs = _board_rungs({'min_hole_clearance': 0.25})
    low = [(r['via_diameter'], r['via_drill'], r['clearance'], round(_ring_to_hole(r), 4))
           for r in rungs if _ring_to_hole(r) < 0.25 - 1e-9]
    assert rungs and not low, (
        'via rungs (diameter, drill, clearance, copper-to-hole) under the '
        'board min_hole_clearance 0.25 mm: %s' % low)


if __name__ == '__main__':
    test_board_rule_binds_escalated_vias()
    test_rungs_raised_to_hole_clearance()
    test_policy_reads_the_board_rule()
    print('PASS')
