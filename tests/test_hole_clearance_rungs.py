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
    prev_hole = ft.get_hole_clearance()
    try:
        ft.set_default_fab_tier('auto', {})
        ft.set_escalation_policy(policy, {})
        ft.set_hole_clearance(hole)
        return ft.escalation_rungs(4)
    finally:
        ft.set_hole_clearance(prev_hole)
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
    prev_hole = ft.get_hole_clearance()
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
            ft.set_hole_clearance(prev_hole)
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
    prev_hole = ft.get_hole_clearance()
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
            ft.set_hole_clearance(prev_hole)
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


def _config_hole(config):
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    prev_hole = ft.get_hole_clearance()
    try:
        ft.set_fab_tier_from_config(config)
        return ft.get_hole_clearance()
    finally:
        ft.set_hole_clearance(prev_hole)
        ft.set_default_fab_tier(*prev_tier)
        ft.set_escalation_policy(*prev_pol)


def test_gui_config_installs_and_resets_the_floor():
    # the GUI path keeps the same floor as the CLI: the live board's rule, else KiCad's factory value
    prev_hole = ft.get_hole_clearance()
    try:
        assert _config_hole({'escalation': 'board', 'min_hole_clearance': 0.19}) == 0.19
        assert _config_hole({'escalation': 'board'}) == ft.KICAD_FACTORY_HOLE_CLEARANCE
        # a previous run's floor never carries over to the next board
        ft.set_hole_clearance(0.25)
        assert _config_hole({'escalation': 'fab', 'min_hole_clearance': 0.12}) == 0.12
        ft.set_hole_clearance(0.25)
        assert _config_hole({'escalation': 'off', 'min_hole_clearance': 0.3}) is None
    finally:
        ft.set_hole_clearance(prev_hole)


def test_gui_rungs_match_cli_floor():
    """GUI escalation rungs keep KiCad's factory 0.25 mm copper-to-hole floor, as the CLI's do."""
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    prev_hole = ft.get_hole_clearance()
    try:
        ft.set_hole_clearance(None)
        ft.set_fab_tier_from_config({'escalation': 'fab', 'fab_tier': 'auto'})
        rungs = ft.escalation_rungs(4)
        low = sorted(round(_ring_to_hole(r), 4) for r in rungs if _ring_to_hole(r) < 0.25 - 1e-9)
        assert rungs and not low, 'GUI rungs under 0.25 mm copper-to-hole: %s' % low
    finally:
        ft.set_hole_clearance(prev_hole)
        ft.set_default_fab_tier(*prev_tier)
        ft.set_escalation_policy(*prev_pol)


def test_fresh_gui_process_gets_the_factory_floor():
    """A fresh process (no CLI run before it) configured the GUI way: the
    board's value when given, else KiCad's factory floor."""
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import fab_tiers as ft; "
            "ft.set_fab_tier_from_config({'escalation': 'fab', 'fab_tier': 'auto'}); "
            "a = ft.get_hole_clearance(); "
            "ft.set_fab_tier_from_config({'escalation': 'board', 'min_hole_clearance': 0.19}); "
            "print(a, ft.get_hole_clearance())") % os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'py_router')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.split() == [repr(ft.KICAD_FACTORY_HOLE_CLEARANCE), '0.19'], out.stdout


if __name__ == '__main__':
    test_board_rule_binds_escalated_vias()
    test_rungs_raised_to_hole_clearance()
    test_policy_reads_the_board_rule()
    test_gui_config_installs_and_resets_the_floor()
    test_fresh_gui_process_gets_the_factory_floor()
    test_gui_rungs_match_cli_floor()
    print('PASS')
