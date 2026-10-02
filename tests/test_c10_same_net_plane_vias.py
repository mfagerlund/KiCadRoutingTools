#!/usr/bin/env python3
"""C10 regression: plane joins/oracle welds must honor same-net pad clearance.
Run: python tests/test_c10_same_net_plane_vias.py
Tiny in-memory board, real Rust A*, plus constructor/GUI forwarding contracts.
"""
import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for sub in ('py_router', 'py_tools', 'rust_router', 'tests'):
    sys.path.insert(0, str(ROOT / sub))
from test_581_same_net_pad_via_clearance import _pad, _pcb
from routing_config import GridRouteConfig, GridCoord
from plane_region_connector import build_base_obstacles, route_plane_connection_wide
from obstacle_map import same_net_pad_via_keepout_cells


def main():
    failures = []
    def check(name, ok):
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        if not ok:
            failures.append(name)

    pad = _pad(6, 6)
    back = _pad(6, 6)
    back.layers = ['B.Cu']
    pcb = _pcb([pad, back])
    for gap in (-1.0, 0.0, 0.1):
        cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.05,
                              track_width=0.15, clearance=0.15,
                              via_size=0.45, via_drill=0.2,
                              same_net_pad_clearance=gap)
        obs, _ = build_base_obstacles({1}, cfg.layers, pcb, cfg, 0.15, 0.15)
        check(f'{gap}: pad centre via block', obs.is_via_blocked(120, 120) == (gap > 0))
        check(f'{gap}: pad remains track reachable', not obs.is_blocked(120, 120, 0))
        result, _ = route_plane_connection_wide(
            [(6, 6, 'F.Cu')], [(6, 6, 'B.Cu')], 0, cfg.layers, obs, cfg, [],
            max_iterations=10000)
        check(f'{gap}: layer-changing route succeeds', bool(result and result[1]))
        if result:
            vias = result[1]
            check(f'{gap}: one new via', len(vias) == 1)
            if gap > 0:
                # Independent continuous rectangle distance, not the map helper.
                check('via copper edge >= 0.1mm from pad', all(
                    (max(abs(x-6)-0.5, 0)**2 + max(abs(y-6)-0.5, 0)**2)**0.5
                    - cfg.via_size/2 >= gap-1e-9 for x, y in vias))
            else:
                check(f'{gap}: legacy in-pad via preserved', vias == [(6.0, 6.0)])
        if gap > 0:
            keep = same_net_pad_via_keepout_cells(pcb, 1, cfg)
            check('whole pad margin stamped', all(obs.is_via_blocked(int(x), int(y)) for x,y in keep))
            reused, _ = route_plane_connection_wide(
                [(6, 6, 'F.Cu')], [(6, 6, 'B.Cu')], 0, cfg.layers, obs, cfg, [(6, 6)],
                max_iterations=10000)
            check('existing barrel reused without new via', reused is not None and not reused[1])
            # The direct stitch fallback must reject this same blocked site.
            tree = ast.parse((ROOT/'py_router/kicad_oracle.py').read_text(encoding='utf-8'))
            assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == '_ok649' for t in n.targets)
                           and not (isinstance(n.value, ast.Constant) and n.value.value is False)]
            check('one direct-stitch initial guard', len(assignments) == 1)
            for x, want in ((6, False), (8, True)):
                env = dict(config=cfg, base_obstacles=obs, GridCoord=GridCoord, _vx=x, _vy=6)
                got = eval(compile(ast.Expression(assignments[0].value), 'stitch-guard', 'eval'), env)
                check(f'direct stitch at {x}: {want}', got == want)

    # Execute the actual constructor expressions, so defaults cannot mask a
    # dropped field (the CLI finalize and capacity-oracle were both affected).
    for filename, names in (('py_router/route.py', {'_ocfg','_cap_cfg'}),):
        tree = ast.parse((ROOT/filename).read_text(encoding='utf-8'))
        found = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
                continue
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    found.add(t.id)
                    expr = ast.Expression(n.value)
                    got = eval(compile(expr, filename, 'eval'),
                               dict(GridRouteConfig=GridRouteConfig, config=cfg, _oedge=0.2))
                    check(t.id+' retains active clearance', got.same_net_pad_clearance == 0.1)
        check('both CLI oracle constructors covered', found == names)
    # Standalone repair: an explicit 0/-1 must override a persisted positive
    # value, while an omitted flag inherits the source project.
    from types import SimpleNamespace
    tree = ast.parse((ROOT/'py_router/repair_planes.py').read_text(encoding='utf-8'))
    call = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '_ocfg' for t in n.targets))
    for flag, expected in ((None, 0.2), (-1.0, -1.0), (0.0, 0.0), (0.1, 0.1)):
        args = SimpleNamespace(same_net_pad_clearance=flag, input_file='fixture',
            clearance=0.15, track_width=0.15, via_size=0.45, via_drill=0.2,
            grid_step=0.05, layers=['F.Cu', 'B.Cu'], layer_costs=[])
        got = eval(compile(ast.Expression(call), 'repair-config', 'eval'), dict(
            args=args, GridRouteConfig=GridRouteConfig, _oracle_edge=0.2,
            read_snpc_for_pcb_data=lambda *_: 0.2))
        check(f'repair flag {flag} resolves to {expected}', got.same_net_pad_clearance == expected)
    for filename, token in (
        ('py_router/repair_planes.py', 'same_net_pad_clearance=(args.same_net_pad_clearance'),
        ('py_router/route.py', "'same_net_pad_clearance': config.same_net_pad_clearance"),
        ('kicad_routing_plugin/gui_utils.py', 'same_net_pad_clearance=same_net_pad_clearance'),
        ('kicad_routing_plugin/swig_gui.py', "same_net_pad_clearance=_pfo.get('same_net_pad_clearance', -1.0)")):
        check(filename+' forwards clearance', token in (ROOT/filename).read_text(encoding='utf-8'))
    print(f'{len(failures)} failure(s)')
    return bool(failures)

if __name__ == '__main__':
    sys.exit(main())
