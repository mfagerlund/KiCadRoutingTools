"""#NNNN: the finer-grid rung (#1763) routes at the board floor's width.

The rung re-routed boxed-in nets at a finer grid with rip authority, but at
the run's nominal track and via. A net whose pads sit off-grid at fine pitch
needs all three at once: the finer grid, the narrower floor track and rip
authority over the nets walling it. The per-net rescue has the fine grid and
the floor width but no rip authority; the terminal escalation has the floor
width but the run's grid. #1763's fixture (0.5 mm-pitch QFN, eight nets, a
0.28 clearance rule) at a nominal 0.15 track left all four boxed-in nets open
after the rung; at the floor width the rung recovers at least three.

Under ``--escalation off`` nothing narrows: the rung keeps the run's geometry.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_utils import ROOT_DIR, tool, tool_env, evidence  # noqa: E402
from test_1763_finer_grid_rung import _make_fixture  # noqa: E402

sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))
import fab_tiers  # noqa: E402
import finer_grid_rung as fgr  # noqa: E402


def test_floor_geometry_empty_under_escalation_off():
    fab_tiers.set_escalation_policy('off', writeback=False)
    try:
        assert fgr.floor_geometry(None, None, [1], 0.2, 0.4) == {}
    finally:
        fab_tiers.set_escalation_policy(None, writeback=False)


def test_floor_geometry_no_nets():
    assert fgr.floor_geometry(None, None, [], 0.2, 0.4) == {}


def _route(d, *extra):
    qfn = _make_fixture(d)
    out = os.path.join(d, 'out.kicad_pcb')
    js = os.path.join(d, 'out.json')
    r = subprocess.run(
        [sys.executable, '-B', '-X', 'utf8', tool('route.py'), qfn, out,
         '--nets', 'N*', '--track-width', '0.15', '--clearance', '0.28',
         '--grid-step', '0.1', '--via-size', '0.3', '--via-drill', '0.15',
         '--json-out', js] + list(extra),
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', env=tool_env(), cwd=ROOT_DIR)
    log = r.stdout + r.stderr
    assert r.returncode == 0, log[-3000:]
    evidence(js)
    summ = json.load(open(js, encoding='utf-8'))
    failed = (set(summ.get('failed_single') or [])
              | set(summ.get('open_single') or [])
              | {m['net_name'] for m in summ.get('failed_multipoint') or []})
    rung = [ln for ln in log.splitlines() if 'Finer-grid rung (#1763)' in ln]
    return log, rung, failed, summ, out


def _track_widths(path):
    import re
    s = open(path, encoding='utf-8').read()
    return [float(w) for w in re.findall(
        r'\(segment\b[^()]*(?:\([^()]*\)[^()]*)*?\(width ([\d.]+)\)', s)]


def test_rung_routes_at_floor_width():
    d = tempfile.mkdtemp(prefix='tNNNN_')
    try:
        log, rung, failed, summ, out = _route(d)
        assert rung and 'at grid 0.05' in rung[0], log[-3000:]
        assert ' at track ' in rung[0], rung[0]
        floor = float(rung[0].split(' at track ')[1].split()[0])
        assert floor < 0.15, rung[0]
        # 4 boxed in before the rung; it recovers at least 3 of them
        assert len(failed) <= 1, failed
        assert summ.get('min_clearance_used') == 0.28
        widths = _track_widths(out)
        assert widths and min(widths) >= floor - 1e-6, (min(widths), floor)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rung_keeps_width_under_escalation_off():
    d = tempfile.mkdtemp(prefix='tNNNN_off_')
    try:
        log, rung, failed, summ, out = _route(d, '--escalation', 'off')
        for ln in rung:
            assert ' at track ' not in ln and ' via ' not in ln, ln
        widths = _track_widths(out)
        assert not widths or min(widths) >= 0.15 - 1e-6, min(widths)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    for fn in (test_floor_geometry_empty_under_escalation_off,
               test_floor_geometry_no_nets,
               test_rung_routes_at_floor_width,
               test_rung_keeps_width_under_escalation_off):
        fn()
        print(f'PASS {fn.__name__}')
