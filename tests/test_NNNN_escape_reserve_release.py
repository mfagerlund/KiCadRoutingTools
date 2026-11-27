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
     into); with a pour it keeps its plane via.
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


def main():
    d = tempfile.mkdtemp(prefix='tNNNN_err_')
    try:
        errs = (check_failed_release(d) + check_used_bones(d)
                + check_power_without_zone(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
