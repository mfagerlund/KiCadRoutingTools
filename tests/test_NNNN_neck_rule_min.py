#!/usr/bin/env python3
"""A neck-down never narrows a net below its .kicad_dru track_width minimum.

A net whose rule fixes its width (min = max, an impedance class) routes wider
than the nominal track width. When the wide route is blocked, the neck-down
used to fall back to the nominal width on every layer, ignoring the rule
minimum, and KiCad's DRC graded every necked segment as a track_width error.

Geometry: a closed single-layer corridor (net-2 walls) whose gap fits the
0.1 mm nominal width and 0.15 mm, but not 0.25 mm (0.05 mm grid).

  1. Rule min 0.25 = net width: no neck is legal, the edge must fail.
  2. Rule min 0.15 under a 0.25 net width: the neck may narrow to 0.15
     and no further; every drawn segment is at least 0.15.
  3. No rule min (control): the neck still narrows below 0.15.
  4. Cases 1-2 again on a long trunk (the single narrow retry).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_tools'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rust_router'))

from grid_router import GridRouter, GridObstacleMap
from routing_config import GridRouteConfig
from obstacle_map import add_segments_list_as_obstacles
from single_ended_routing import _route_main_connection, _neck_width_for_net
from synth import make_pcb, make_net, make_seg

CHECKS = []


def check(name, ok):
    CHECKS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


def _config(rule_min):
    cfg = GridRouteConfig(track_width=0.1, clearance=0.1, grid_step=0.05,
                          layers=['F.Cu'])
    cfg.net_layer_widths = {1: {'F.Cu': 0.25}}
    if rule_min is not None:
        cfg.net_layer_min_widths = {1: {'F.Cu': rule_min}}
    return cfg


def _route(cfg, length=10.0):
    walls = [
        make_seg(0.0, 0.3, length, 0.3, net_id=2, width=0.2, layer='F.Cu'),
        make_seg(0.0, -0.3, length, -0.3, net_id=2, width=0.2, layer='F.Cu'),
        make_seg(0.0, -0.3, 0.0, 0.3, net_id=2, width=0.2, layer='F.Cu'),
        make_seg(length, -0.3, length, 0.3, net_id=2, width=0.2, layer='F.Cu'),
    ]
    obstacles = GridObstacleMap(len(cfg.layers))
    add_segments_list_as_obstacles(obstacles, walls, cfg)
    pcb = make_pcb(nets={1: make_net(1, 'SIG1'), 2: make_net(2, 'WALL')},
                   segments=walls)
    router = GridRouter(via_cost=50, h_weight=1.5, turn_cost=100)
    res = _route_main_connection(router, obstacles, cfg, [(40, 0, 0)], [(int(length / cfg.grid_step) - 40, 0, 0)],
                                 cfg.track_margins_for_net(1), pcb, 1)
    return res[0], res[-2], res[-1]  # path, necked_down, uniform_width


def _drawn_width(cfg, necked, uniform):
    """The width the segments of this result are drawn at."""
    if uniform is not None:
        return uniform
    if necked:
        return _neck_width_for_net(cfg, 1, 'F.Cu')
    return cfg.get_net_track_width(1, 'F.Cu')


def main():
    print("neck-down honours the track_width rule minimum")

    cfg = _config(0.25)
    path, necked, uniform = _route(cfg)
    check("rule min = net width: no neck, the edge fails",
          path is None or _drawn_width(cfg, necked, uniform) >= 0.25 - 1e-9)

    cfg = _config(0.15)
    path, necked, uniform = _route(cfg)
    w = _drawn_width(cfg, necked, uniform) if path is not None else None
    check(f"rule min 0.15: routes necked, drawn width {w} >= 0.15",
          path is not None and w is not None and w >= 0.15 - 1e-9)

    cfg = _config(None)
    path, necked, uniform = _route(cfg)
    w = _drawn_width(cfg, necked, uniform) if path is not None else None
    check(f"no rule min: necks below 0.15 (drawn {w})",
          path is not None and w is not None and w < 0.15 - 1e-9)

    cfg = _config(0.15)
    path, necked, uniform = _route(cfg, length=30.0)
    w = _drawn_width(cfg, necked, uniform) if path is not None else None
    check(f"long trunk, rule min 0.15: necked, drawn width {w} >= 0.15",
          path is not None and necked and w is not None and w >= 0.15 - 1e-9)

    cfg = _config(0.25)
    path, necked, uniform = _route(cfg, length=30.0)
    check("long trunk, rule min = net width: the edge fails", path is None)

    cfg = _config(0.15)
    check("_neck_width_for_net floors at the rule min",
          abs(_neck_width_for_net(cfg, 1, 'F.Cu') - 0.15) < 1e-9)

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
