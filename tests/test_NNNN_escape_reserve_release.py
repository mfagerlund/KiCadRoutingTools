#!/usr/bin/env python3
"""Escape reservation, release rules: a reserved dog-bone must not outlive
its use and block the neighbours.

Fixture: the fine-pitch row of test_NNNN_escape_reserve_qfn (N signal pads
in a column, each net needing one via, every pin planned a dog-bone).

Checks:
  1. a dog-bone net whose route fails has its reservation taken off the
     board (and out of the bound obstacle maps) when the next net's bracket
     opens, so the neighbours route around bare pads; a net that routes
     again at once adopts its own reservation instead;
  2. a successful route keeps only the bones it uses: a bone whose via or
     stub the route's copper reaches is committed, a bone beside a pad the
     route reached its own way is dropped, a bone of a pad the route never
     touched is kept (a later tap may land there);
  3. no fresh dog-bone is laid after the pre-pass: a net whose reservation
     was released routes against bare pads (a fresh lay sees only pcb_data,
     not copper held only in the obstacle maps, and could short it);
  4. a power-like net with no pour gets no reservation (no plane to drop
     into); with a pour it keeps its plane via, and the kept bone is in the
     written file (the writer emits only its write-lists, never pcb_data);
  5. a large other-net pad whose centre lies beyond the search span but whose
     edge is within clearance blocks a via site and a stub;
  6. a net fine_escape already reserved is neither planned nor marked a
     dog-bone net: it keeps its stub handling;
  7. reserved_copper names the dog-bone vias, so the run's input-via
     snapshot leaves them out.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
import test_NNNN_escape_reserve_qfn as qfn  # noqa: E402


def _armed(d, name, rename=None):
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    import fine_escape as fe
    pcb = parse_kicad_pcb(qfn._write(d, name))
    cfg = qfn._config()
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    if rename:
        for nid in ids[:1]:
            for p in pcb.pads_by_net[nid]:
                p.net_name = rename
            pcb.nets[nid].name = rename
    with contextlib.redirect_stdout(io.StringIO()):
        er.escape_reserve_prepass(pcb, cfg, ids)
    st = getattr(pcb, fe._STATE)
    fe.attach_routing_state(pcb, types.SimpleNamespace(working_obstacles=None,
                                            net_obstacles_cache=None), cfg)
    return pcb, cfg, ids, st


def _on_board(pcb, objs):
    have = {id(o) for o in list(pcb.segments) + list(pcb.vias)}
    return [o for o in objs if id(o) in have]


def check_failed_release(d):
    import fine_escape as fe
    pcb, cfg, ids, st = _armed(d, 'fail.kicad_pcb')
    a, b, c = sorted(st.pending)[:3]
    objs_a = list(st.pending[a])
    errs = []
    fe.finish(pcb, fe.begin(pcb, a, cfg), {'failed': True})
    tok = fe.begin(pcb, a, cfg)  # the same net again: adopts its own
    if tok is None or tok[1] != objs_a:
        errs.append("failed release: a net routing again did not adopt its reservation")
    fe.finish(pcb, tok, {'failed': True})
    tok = fe.begin(pcb, b, cfg)  # the next net: a's reservation goes
    if a in st.pending or _on_board(pcb, objs_a):
        errs.append("failed release: a failed net's reservation still blocks "
                    "when the next net routes")
    fe.finish(pcb, tok, {'failed': True})
    # 3: no fresh lay for the released net
    if (fe.begin(pcb, a, cfg) is not None
            or any(o.net_id == a for o in list(pcb.segments) + list(pcb.vias))):
        errs.append("fresh lay: a released dog-bone net laid fresh copper")
    print(f"  failed net released at the next bracket, no fresh lay: "
          f"{'ok' if not errs else 'NO'}")
    return errs


def check_used_bones(d):
    from kicad_parser import Segment
    import fine_escape as fe
    pcb, cfg, ids, st = _armed(d, 'used.kicad_pcb')
    errs = []
    nets = sorted(st.pending)
    # route reaches the via: bone kept
    a = nets[0]
    objs = list(st.pending[a])
    via = next(o for o in objs if fe._is_via(o))
    far = Segment(start_x=via.x, start_y=via.y, end_x=via.x + 3, end_y=via.y,
                  width=0.2, layer='B.Cu', net_id=a)
    res = fe.finish(pcb, fe.begin(pcb, a, cfg), {'new_segments': [far], 'new_vias': []})
    if not any(o is via for o in res.get('new_vias') or []):
        errs.append("used bones: a bone the route reaches was not committed")
    # route reaches the pad its own way: bone dropped
    b = nets[1]
    objs = list(st.pending[b])
    pad = min(pcb.pads_by_net[b], key=lambda p: abs(p.global_x - qfn.ROW_X))
    own = Segment(start_x=pad.global_x, start_y=pad.global_y,
                  end_x=pad.global_x - 3, end_y=pad.global_y,
                  width=0.2, layer='F.Cu', net_id=b)
    res = fe.finish(pcb, fe.begin(pcb, b, cfg), {'new_segments': [own], 'new_vias': []})
    if (res.get('new_vias') or [o for o in res['new_segments'] if o is not own]
            or _on_board(pcb, objs)):
        errs.append("used bones: an unused bone beside a reached pad was committed")
    # route touches nothing of the part: bone kept
    c = nets[2]
    objs = list(st.pending[c])
    res = fe.finish(pcb, fe.begin(pcb, c, cfg), {'new_segments': [], 'new_vias': []})
    if len(res.get('new_vias') or []) + len(res['new_segments']) != len(objs):
        errs.append("used bones: a bone of an untouched pad was dropped")
    print(f"  used bones kept, unused beside a reached pad dropped: "
          f"{'ok' if not errs else 'NO'}")
    return errs


def check_power_without_zone(d):
    pcb, cfg, ids, st = _armed(d, 'pwr.kicad_pcb', rename='VDD')
    errs = []
    if ids[0] in st.pending:
        errs.append("power: a power-like net with no pour got a reservation")
    if len(st.pending) != len(ids) - 1:
        errs.append(f"power: {len(st.pending)} signal reservations, want {len(ids) - 1}")
    print(f"  power-like net with no pour not reserved: {'ok' if not errs else 'NO'}")
    return errs


def check_reserved_vias_named(d):
    import fine_escape as fe
    pcb, cfg, ids, st = _armed(d, 'named.kicad_pcb')
    seg_ids, via_ids = fe.reserved_copper(pcb)
    bones = [o for g in st.pending.values() for o in g if fe._is_via(o)]
    errs = []
    if not bones or {id(v) for v in bones} != via_ids:
        errs.append("reserved vias: reserved_copper does not name the dog-bone vias")
    if via_ids & seg_ids:
        errs.append("reserved vias: a via is named as a segment")
    print(f"  reserved_copper names the dog-bone vias: {'ok' if not errs else 'NO'}")
    return errs


def check_plane_bone_written(d):
    from kicad_parser import parse_kicad_pcb
    from output_writer import write_routed_output
    import fine_escape as fe
    pcb, cfg, ids, st = _armed(d, 'plane.kicad_pcb')
    errs = []
    nid = sorted(st.pending)[0]
    st.plane.add(nid)  # as if the net had a pour
    bone_vias = [o for o in st.pending[nid] if fe._is_via(o)]
    segs, vias = [], []
    with contextlib.redirect_stdout(io.StringIO()):
        fe.release(pcb, config=cfg, ship=(segs, vias))
    if not bone_vias or [id(v) for v in vias] != [id(v) for v in bone_vias]:
        errs.append("plane bone: release did not hand the kept via to the write-list")
    src = os.path.join(d, 'plane.kicad_pcb')
    out = os.path.join(d, 'plane_out.kicad_pcb')
    with contextlib.redirect_stdout(io.StringIO()):
        write_routed_output(src, out, [], [], vias, [], [], [], pcb,
                            all_swap_segments=segs)
    got = parse_kicad_pcb(out)
    for v in bone_vias:
        if not any(w.net_id == nid and abs(w.x - v.x) < 1e-4 and abs(w.y - v.y) < 1e-4
                   for w in got.vias):
            errs.append(f"plane bone: kept via at ({v.x:.3f}, {v.y:.3f}) not in the written file")
    print(f"  pour net's kept dog-bone written: {'ok' if not errs else 'NO'}")
    return errs


def check_large_pad_in_reach(d):
    import copy
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    pcb = parse_kicad_pcb(qfn._write(d, 'big.kicad_pcb'))
    cfg = qfn._config()
    errs = []
    sig = next(nid for nid, n in pcb.nets.items() if n.name.startswith('S'))
    other = next(nid for nid, n in pcb.nets.items() if n.name == 'WALL')
    x, y = qfn.ROW_X - 3.3, 110.0  # left of the exposed pad, clear of the edge
    board = er._Board(pcb, cfg)
    if not board.via_ok(sig, x, y, cfg.clearance) or not board.stub_ok(
            sig, (x, y), (x + 0.2, y), cfg.track_width, 'F.Cu', cfg.clearance, None):
        errs.append("large pad: the control site is not legal on the bare board")
    big = copy.copy(next(q for q in board.pads if q.net_id == other))
    # centre 3.5 mm off (beyond the 3 mm span), edge 0.15 mm from the site
    big.global_x, big.global_y = x, y + 3.5
    big.size_x, big.size_y, big.shape = 2.0, 6.7, 'rect'
    big.rect_rotation = 0.0
    big.layers = ['F.Cu', 'B.Cu']
    big.drill = 0
    board.pads = board.pads + [big]
    if board.via_ok(sig, x, y, cfg.clearance):
        errs.append("large pad: a via site 0.15 mm from a big pad's edge passed")
    if board.stub_ok(sig, (x, y), (x + 0.2, y), cfg.track_width, 'F.Cu',
                     cfg.clearance, None):
        errs.append("large pad: a stub 0.15 mm from a big pad's edge passed")
    print(f"  large pad beyond the span still blocks: {'ok' if not errs else 'NO'}")
    return errs


def check_fine_escape_net_kept(d):
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    import fine_escape as fe
    pcb = parse_kicad_pcb(qfn._write(d, 'both.kicad_pcb'))
    cfg = qfn._config()
    ids = sorted(nid for nid, n in pcb.nets.items() if n.name.startswith('S'))
    st = fe._State({ids[0]}, fe._footprints(pcb), fe._all_pads(pcb), None, False)
    held = []
    st.pending[ids[0]] = held  # fine_escape's stub reservation
    setattr(pcb, fe._STATE, st)
    with contextlib.redirect_stdout(io.StringIO()):
        er.escape_reserve_prepass(pcb, cfg, ids)
    errs = []
    if st.pending.get(ids[0]) is not held:
        errs.append("both on: a fine_escape net's reservation was replaced")
    if ids[0] in st.dogbone:
        errs.append("both on: a fine_escape net was marked a dog-bone net")
    if not all(n in st.dogbone for n in st.pending if n != ids[0]):
        errs.append("both on: a reserved net is not a dog-bone net")
    if any(n not in st.pending for n in st.dogbone):
        errs.append("both on: a net with no reservation was marked a dog-bone net")
    print(f"  fine_escape net keeps its stub handling: {'ok' if not errs else 'NO'}")
    return errs


def main():
    d = tempfile.mkdtemp(prefix='tNNNN_err_')
    try:
        errs = (check_failed_release(d) + check_used_bones(d)
                + check_power_without_zone(d) + check_reserved_vias_named(d)
                + check_plane_bone_written(d)
                + check_large_pad_in_reach(d) + check_fine_escape_net_kept(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
