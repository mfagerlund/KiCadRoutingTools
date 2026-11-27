#!/usr/bin/env python3
"""Escape reservation: walled-in pins and the copper-to-hole floor.

1. Flanked pins. A power-like net with no pour is not reserved (it routes
   like a signal). But when one such net holds both row neighbours of a pin
   (pins A B A), the net joins its pins along the row edge and walls the
   middle pin in; once that track exists neither the floors nor the retry
   can rip it. The middle pin keeps its dog-bone even when its own net is a
   pour-less power net. Pins with no such neighbours stay unreserved.

2. Hole clearance. A dog-bone via is a drilled hole: it must keep the
   board's copper-to-hole floor (min_hole_clearance) from other-net pads,
   not only the copper-to-copper clearance from its ring. With a hole floor
   larger than ring clearance, a site that only meets the ring rule is
   illegal.

Fixture: a single column of 0.5 mm pitch pads on F.Cu; each net ends on a
B.Cu pad 9 mm away (far, other layer: every pin needs a via).
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from run_utils import evidence  # noqa: E402

PITCH = 0.5
ROW_X = 110.0


def _smd(num, x, y, sx, sy, net, name, shape, layer):
    return (f'\t\t(pad "{num}" smd {shape}\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "{layer}")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _fp(ref, x, y, pads):
    return (f'\t(footprint "test:{ref}"\n\t\t(layer "F.Cu")\n'
            f'\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -5 0)\n'
            f'\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
            + ''.join(pads) + '\t)\n')


def board_text(pins):
    """`pins`: net name per row pin, top to bottom."""
    names = sorted(set(pins))
    ids = {n: 1 + i for i, n in enumerate(names)}
    nets = ['(net 0 "")'] + [f'(net {ids[n]} "{n}")' for n in names]
    span = (len(pins) - 1) * PITCH
    u1, j1 = [], []
    for k, n in enumerate(pins):
        y = -span / 2 + k * PITCH
        u1.append(_smd(str(k + 1), 0, y, 0.8, 0.25, ids[n], n, 'roundrect', 'F.Cu'))
        j1.append(_smd(str(k + 1), 9.0, y, 0.3, 0.3, ids[n], n, 'circle', 'B.Cu'))
    half_h = span / 2 + 3.0
    body = _fp('U1', ROW_X, 110, u1) + _fp('J1', ROW_X, 110, j1)
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_NNNN_escape_reserve_flank")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body +
            f'\t(gr_rect\n\t\t(start {ROW_X - 4:.4f} {110 - half_h:.4f})\n'
            f'\t\t(end {ROW_X + 11:.4f} {110 + half_h:.4f})\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _load(d, name, pins):
    from kicad_parser import parse_kicad_pcb
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text(pins))
    return parse_kicad_pcb(evidence(p))


def _config(**kw):
    from routing_config import GridRouteConfig
    args = dict(track_width=0.2, clearance=0.15, via_size=0.6, via_drill=0.3,
                grid_step=0.05, layers=['F.Cu', 'B.Cu'])
    args.update(kw)
    return GridRouteConfig(**args)


def _reserved(pcb, cfg):
    import escape_reserve as er
    import fine_escape as fe
    ids = [nid for nid in pcb.nets if nid]
    with contextlib.redirect_stdout(io.StringIO()):
        er.escape_reserve_prepass(pcb, cfg, ids)
    st = getattr(pcb, fe._STATE)
    return sorted(pcb.nets[nid].name for nid in st.pending)


def check_flanked(d):
    errs = []
    pins = ['SIG1', 'VDD_A', 'VDD_B', 'VDD_A', 'VDD_C', 'VDD_A', 'SIG2']
    got = _reserved(_load(d, 'flank.kicad_pcb', pins), _config())
    want = ['SIG1', 'SIG2', 'VDD_B', 'VDD_C']
    print(f"  A B A C A row: reserved {', '.join(got) or '-'} (want {', '.join(want)})")
    if got != want:
        errs.append(f"flanked: reserved {got}, want {want} (the pins walled in "
                    f"by VDD_A keep their dog-bones; VDD_A itself none)")
    pins = ['SIG1', 'VDD_A', 'VDD_A', 'VDD_B', 'VDD_C', 'VDD_A', 'SIG2']
    got = _reserved(_load(d, 'free.kicad_pcb', pins), _config())
    want = ['SIG1', 'SIG2']
    print(f"  unflanked row: reserved {', '.join(got) or '-'} (want {', '.join(want)})")
    if got != want:
        errs.append(f"unflanked: reserved {got}, want {want} (pour-less power "
                    f"nets with free neighbours stay unreserved)")
    return errs


def check_hole(d):
    import escape_reserve as er
    pcb = _load(d, 'hole.kicad_pcb', ['SIG1', 'SIG2', 'SIG3'])
    cfg = _config(via_size=0.35, via_drill=0.2, clearance=0.1, hole_clearance=0.3)
    board = er._Board(pcb, cfg)
    q = next(p for p in pcb.pads_by_net[next(n for n, v in pcb.nets.items()
                                              if v.name == 'SIG2')]
             if p.component_ref == 'U1')
    # Ring rule: via ring 0.175 + clearance 0.1 = 0.275 from the pad edge.
    # Hole rule: drill 0.1 + hole floor 0.3 = 0.4. Site 0.33 off the pad's
    # right edge (pad half-width 0.4) meets the first only.
    x = q.global_x + 0.4 + 0.33
    sig1 = next(n for n, v in pcb.nets.items() if v.name == 'SIG1')
    ok = board.via_ok(sig1, x, q.global_y, 0.1)
    far_ok = board.via_ok(sig1, q.global_x + 0.4 + 0.45, q.global_y, 0.1)
    print(f"  via 0.33 mm off another net's pad, hole floor 0.30: "
          f"{'legal' if ok else 'refused'}; 0.45 mm: {'legal' if far_ok else 'refused'}")
    errs = []
    if ok:
        errs.append("hole: a dog-bone via site inside the copper-to-hole floor was legal")
    if not far_ok:
        errs.append("hole: a site clear of the hole floor was refused")
    return errs


def main():
    d = tempfile.mkdtemp(prefix='t_er_flank_')
    try:
        errs = check_flanked(d) + check_hole(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else "FAILED")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
