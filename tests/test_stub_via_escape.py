"""Legal-site stub-via escape for a boxed SMD pad (stub_via_escape).

A fine-pitch connector row whose neighbours are already routed leaves no via
room AT the pad or one pitch off it (the #666 tap / fanout ladder). The
escape walks a pad-layer stub out to the nearest via site that holds every
rule against the copper on the board. Each case builds a small synthetic
board, asks for sites, and re-grades the chosen site INDEPENDENTLY (exact
point/segment distances, check_drc's pad shape), so a site that the search
calls clear but that breaks a rule fails here.

  1. a 1-pad boxed row (0.5 mm pitch, neighbours routed straight then
     fanned): a clear site exists, its via is out of the pad's pocket, and it
     holds clearance, hole clearance and the via minimums;
  2. a connector-row crop (0.4 mm pitch, 0.2 mm pads, 0.1 mm neighbours,
     keep-out behind the row): same;
  3. the shift classification: a corridor whose only via site is taken by
     ONE foreign via reports that site with exactly that net as a movable
     blocker; a site blocked by a pad is hard.

Fails on a fork without the stub_via_escape module.
"""
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'py_router'))
sys.path.insert(0, HERE)

from test_1260_pour_guard import _HEAD, _edge  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (f'  ({detail})' if detail else ''))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- board text
def _fp(ref, x, y, pads, layer='F.Cu'):
    side = 'F' if layer == 'F.Cu' else 'B'
    s = ('\t(footprint ""\n\t\t(layer "%s")\n\t\t(at %g %g)\n'
         '\t\t(property "Reference" "%s"\n\t\t\t(at 0 0 0)\n'
         '\t\t\t(layer "%s.SilkS")\n\t\t\t(effects\n\t\t\t\t(font\n'
         '\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
         % (layer, x, y, ref, side))
    for num, dx, dy, w, h, net in pads:
        s += ('\t\t(pad "%s" smd rect\n\t\t\t(at %g %g)\n\t\t\t(size %g %g)\n'
              '\t\t\t(layers "%s.Cu" "%s.Mask" "%s.Paste")\n\t\t\t(net "%s")\n'
              '\t\t)\n' % (num, dx, dy, w, h, side, side, side, net))
    return s + '\t)\n'


def _seg(x1, y1, x2, y2, w, net, layer='F.Cu'):
    return ('\t(segment\n\t\t(start %g %g)\n\t\t(end %g %g)\n\t\t(width %g)\n'
            '\t\t(layer "%s")\n\t\t(net "%s")\n\t)\n'
            % (x1, y1, x2, y2, w, layer, net))


def _via(x, y, size, drill, net):
    return ('\t(via\n\t\t(at %g %g)\n\t\t(size %g)\n\t\t(drill %g)\n'
            '\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "%s")\n\t)\n'
            % (x, y, size, drill, net))


def _keepout(x0, y0, x1, y1):
    return ('\t(zone\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(name "KO")\n'
            '\t\t(hatch edge 0.5)\n\t\t(connect_pads\n\t\t\t(clearance 0)\n'
            '\t\t)\n\t\t(min_thickness 0.25)\n\t\t(keepout\n'
            '\t\t\t(tracks not_allowed)\n\t\t\t(vias not_allowed)\n'
            '\t\t\t(pads allowed)\n\t\t\t(copperpour not_allowed)\n'
            '\t\t\t(footprints allowed)\n\t\t)\n'
            '\t\t(fill\n\t\t\t(thermal_gap 0.5)\n'
            '\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n'
            '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy %g %g) (xy %g %g) '
            '(xy %g %g) (xy %g %g)\n\t\t\t)\n\t\t)\n\t)\n'
            % (x0, y0, x1, y0, x1, y1, x0, y1))


CX, CY = 20.0, 15.0


def _row_board(path, *, pitch, pad_w, pad_h, n, straight, tw, fan=True,
               extra=''):
    """J1: a row of n pads, SIG in the middle, every other pad's net routed
    straight down `straight` mm then fanned 45 degrees away; a keep-out
    behind the row (as a connector body's courtyard would)."""
    mid = n // 2
    pads = [(str(i + 1), (i - mid) * pitch, 0, pad_w, pad_h,
             'SIG' if i == mid else 'N%d' % i) for i in range(n)]
    parts = [_HEAD, _fp('J1', CX, CY, pads),
             _fp('J2', CX, CY + 10, [('1', 0, 0, 1, 1, 'SIG')], 'B.Cu')]
    for i in range(n):
        if i == mid:
            continue
        x = CX + (i - mid) * pitch
        sgn = 1 if i > mid else -1
        y1 = CY + straight
        parts.append(_seg(x, CY, x, y1, tw, 'N%d' % i))
        if fan:
            parts.append(_seg(x, y1, x + sgn * 2, y1 + 2, tw, 'N%d' % i))
            parts.append(_seg(x + sgn * 2, y1 + 2, x + sgn * 2, CY + 6, tw,
                              'N%d' % i))
    parts.append(_keepout(CX - 4, CY - 3, CX + 4, CY - pad_h / 2 - 0.05))
    corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
    for i in range(4):
        parts.append(_edge(*corners[i], *corners[(i + 1) % 4]))
    parts.append(extra)
    parts.append(')\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(parts))


# ---------------------------------------------------------------- helpers
def _load(path):
    import contextlib
    import io
    from kicad_parser import parse_kicad_pcb
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_kicad_pcb(path)


def _cfg(**kw):
    from routing_config import GridRouteConfig
    base = dict(layers=['F.Cu', 'B.Cu'], track_width=0.15, clearance=0.1,
                via_size=0.6, via_drill=0.3, grid_step=0.05)
    base.update(kw)
    return GridRouteConfig(**base)


def _sig(pcb):
    nid = next(n for n, net in pcb.nets.items() if net.name == 'SIG')
    pad = next(p for p in pcb.pads_by_net[nid] if p.component_ref == 'J1')
    return nid, pad


def _regrade(pcb, site, net_id, cfg):
    """Independent grade of a site: list of broken rules (empty = legal)."""
    from check_drc import point_to_pad_distance
    from net_rescue import _pt_seg_d, _seg_seg_d
    bad = []
    vx, vy = site.via_xy
    r = site.via_size / 2
    hole_clr = getattr(cfg, 'hole_clearance', 0.0) or 0.0
    if site.via_size < 0.4 - 1e-9 or site.via_drill < 0.15 - 1e-9:
        bad.append(f'via {site.via_size}/{site.via_drill} below 0.4/0.15')
    if (site.via_size - site.via_drill) / 2 < 0.075 - 1e-9:
        bad.append('annular below 0.075')
    legs = list(zip(site.path, site.path[1:]))
    for s in pcb.segments:
        if s.net_id == net_id:
            continue
        a, b = (s.start_x, s.start_y), (s.end_x, s.end_y)
        d = _pt_seg_d(vx, vy, *a, *b) - r - s.width / 2
        if d < cfg.clearance - 1e-6:
            bad.append(f'via-seg {d:.3f}')
        dh = _pt_seg_d(vx, vy, *a, *b) - site.via_drill / 2 - s.width / 2
        if dh < hole_clr - 1e-6:
            bad.append(f'hole-seg {dh:.3f}')
        if s.layer == site.layer:
            for p, q in legs:
                g = _seg_seg_d(*p, *q, *a, *b) - site.width / 2 - s.width / 2
                if g < cfg.clearance - 1e-6:
                    bad.append(f'stub-seg {g:.3f}')
    for v in pcb.vias:
        if v.net_id == net_id:
            continue
        d = math.hypot(v.x - vx, v.y - vy)
        if d - r - v.size / 2 < cfg.clearance - 1e-6:
            bad.append(f'via-via {d - r - v.size / 2:.3f}')
        if d - site.via_drill / 2 - v.drill / 2 < 0.25 - 1e-6:
            bad.append('hole-hole')
    for pads in pcb.pads_by_net.values():
        for p in pads:
            if p.net_id == net_id:
                continue
            d = point_to_pad_distance(vx, vy, p) - r
            if d < cfg.clearance - 1e-6:
                bad.append(f'via-pad {p.component_ref}.{p.pad_number} {d:.3f}')
            if site.layer in p.layers or '*.Cu' in p.layers:
                for (x0, y0), (x1, y1) in legs:
                    for t in (i / 20 for i in range(21)):
                        px = x0 + (x1 - x0) * t
                        py = y0 + (y1 - y0) * t
                        dd = point_to_pad_distance(px, py, p) - site.width / 2
                        if dd < cfg.clearance - 1e-6:
                            bad.append(f'stub-pad {p.component_ref}.'
                                       f'{p.pad_number} {dd:.3f}')
                            break
    return bad


# ---------------------------------------------------------------- cases
def _clear_site_case(tag, tmp, **board):
    from stub_via_escape import find_stub_via_sites
    path = os.path.join(tmp, f'{tag}.kicad_pcb')
    _row_board(path, **board)
    pcb = _load(path)
    nid, pad = _sig(pcb)
    cfg = _cfg()
    sites = find_stub_via_sites(pcb, pad, nid, cfg, layer='F.Cu')
    clear = [s for s in sites if not s.blockers]
    check(f'{tag}: a clear stub-via site exists', bool(clear),
          f'{len(sites)} candidate(s)')
    if not clear:
        return
    s = clear[0]
    off = math.hypot(s.via_xy[0] - pad.global_x, s.via_xy[1] - pad.global_y)
    check(f'{tag}: the via is out of the pad pocket (> one pitch)',
          off > board['pitch'] + 1e-6, f'{off:.2f} mm, {s.describe(pcb)}')
    bad = _regrade(pcb, s, nid, cfg)
    check(f'{tag}: the site holds every rule on an independent grade',
          not bad, '; '.join(bad[:4]))
    check(f'{tag}: the stub stays within MAX_STUB_MM',
          s.length <= 2.0 + 1e-6, f'{s.length:.2f}')


def t_synthetic_boxed_row(tmp):
    _clear_site_case('boxed-0.5', tmp, pitch=0.5, pad_w=0.3, pad_h=1.0, n=9,
                     straight=1.2, tw=0.25)


def t_connector_row_crop(tmp):
    _clear_site_case('row-0.4', tmp, pitch=0.4, pad_w=0.2, pad_h=0.8, n=15,
                     straight=1.0, tw=0.1)


def t_shift_classification(tmp):
    """The narrowest corridor's sites carry exactly the one movable via."""
    from stub_via_escape import find_stub_via_sites
    path = os.path.join(tmp, 'shift.kicad_pcb')
    # bar net X across the corridor at y=CY+2.0 and one N5 via sitting where
    # the clear site would be: every remaining site needs N5 moved.
    pre = os.path.join(tmp, 'shift-pre.kicad_pcb')
    _row_board(pre, pitch=0.5, pad_w=0.3, pad_h=1.0, n=9, straight=1.2,
               tw=0.25)
    pcb0 = _load(pre)
    nid0, pad0 = _sig(pcb0)
    clear0 = [s for s in find_stub_via_sites(pcb0, pad0, nid0, _cfg(),
                                             layer='F.Cu') if not s.blockers]
    if not clear0:
        check('shift: the base board has a clear site to occupy', False)
        return
    vx, vy = clear0[0].via_xy
    _row_board(path, pitch=0.5, pad_w=0.3, pad_h=1.0, n=9, straight=1.2,
               tw=0.25, extra=_via(vx, vy, 0.6, 0.3, 'N5'))
    pcb = _load(path)
    nid, pad = _sig(pcb)
    n5 = next(n for n, net in pcb.nets.items() if net.name == 'N5')
    sites = find_stub_via_sites(pcb, pad, nid, _cfg(), layer='F.Cu')
    at = [s for s in sites
          if math.hypot(s.via_xy[0] - vx, s.via_xy[1] - vy) < 1e-6]
    check('shift: the occupied site is still reported', bool(at),
          f'{len(sites)} candidate(s)')
    if at:
        s = at[0]
        check('shift: its only blocker net is N5, movable',
              s.blocker_nets == frozenset({n5}) and not s.hard,
              s.describe(pcb))
    hard = [s for s in sites if s.hard]
    check('shift: pad-blocked sites are hard',
          all(any(b.kind in ('pad', 'drill', 'keepout') for b in s.blockers)
              for s in hard), f'{len(hard)} hard')
    ranked = [s.rank_key() for s in sites]
    check('shift: sites come ranked', ranked == sorted(ranked))


def t_open_ids_candidates(tmp):
    """A pre-existing net rerouted PARTIAL (no pad debt, no restore) is a
    candidate only when the late pass names it in open_ids; existing copper
    never is."""
    from types import SimpleNamespace
    from net_rescue import rescue_candidates
    pcb = SimpleNamespace(nets={1: 'A', 2: 'B'},
                          pads_by_net={1: [0, 1], 2: [0, 1]})
    results = {1: {'path': []}, 2: {'is_existing_route': True}}
    nets = [('A', 1), ('B', 2)]
    check('open_ids: without open_ids a debt-free result is no candidate',
          rescue_candidates(pcb, results, nets) == [])
    got = rescue_candidates(pcb, results, nets, open_ids={1, 2})
    check('open_ids: named debt-free result is partial, existing is not',
          got == [('A', 1, 'partial')], repr(got))


def _late_pass(tmp, tag, diff_pair):
    """Run the late pass (stub_via=True) on the 1-pad boxed row with the tap
    rung instrumented; return (summary, tap calls, SIG's new segments)."""
    import contextlib
    import io
    import plane_pad_tap
    import net_rescue
    path = os.path.join(tmp, f'{tag}.kicad_pcb')
    _row_board(path, pitch=0.5, pad_w=0.3, pad_h=1.0, n=9, straight=1.2,
               tw=0.25)
    pcb = _load(path)
    nid, _pad = _sig(pcb)
    calls = []
    real = plane_pad_tap.tap_pad_with_escalation

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)
    from routing_state import RoutingState
    state = RoutingState(pcb_data=pcb, config=_cfg())
    state.working_obstacles = None
    state.routed_results[nid] = {'path': []}
    state.remaining_net_ids.append(nid)
    if diff_pair:
        state.diff_pair_by_net_id[nid] = ('SIG', None)
    before = sum(1 for sg in pcb.segments if sg.net_id == nid)
    plane_pad_tap.tap_pad_with_escalation = spy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            summary = net_rescue.rescue_failed_nets(
                state, [('SIG', nid)], stub_via=True, open_ids={nid})
    finally:
        plane_pad_tap.tap_pad_with_escalation = real
    after = sum(1 for sg in pcb.segments if sg.net_id == nid)
    return summary, calls, after - before


def t_late_pass_stub_rung_only(tmp):
    """The late pass runs ONLY the stub rung (no tap, no fanout: rerun late
    they lay dogbones later stages must route around -- stmp157 mem_cap), and
    never on a diff-pair member (cm5io: stubs on USB3 legs, pairs lost)."""
    summary, calls, added = _late_pass(tmp, 'late_se', diff_pair=False)
    check('late pass: the tap rung is never called', not calls,
          f'{len(calls)} call(s)')
    check('late pass: a single-ended boxed pad is attempted',
          bool(summary) and summary['attempted'] == 1, repr(summary))
    check('late pass: it gets a stub escape on the board', added > 0,
          f'{added} new segment(s)')
    summary, calls, added = _late_pass(tmp, 'late_dp', diff_pair=True)
    check('late pass: a diff-pair member is not attempted',
          (not summary or summary['attempted'] == 0) and added == 0
          and not calls, repr(summary))


def main():
    try:
        import stub_via_escape  # noqa: F401
    except ImportError as e:
        print(f'FAIL stub_via_escape is missing ({e})')
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        for t in (t_synthetic_boxed_row, t_connector_row_crop,
                  t_shift_classification, t_open_ids_candidates,
                  t_late_pass_stub_rung_only):
            try:
                t(tmp)
            except Exception as e:                          # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f'{t.__name__} raised', False, repr(e))
    print(f'\n{len(failures)} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
