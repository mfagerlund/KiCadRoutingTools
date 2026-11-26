"""Artwork on F.Mask / B.Mask is a solder-mask opening: bare copper under it
is exposed. The router must keep tracks and vias of that side's copper out of
mask artwork (board graphics and pad-less footprint graphics alike)."""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from synth import RoutingBoard  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

# Mask opening spanning both straight paths (y=10 and y=14), x 15..25.
BOX = (15.0, 8.0, 25.0, 16.0)


def _board(artwork):
    b = RoutingBoard(w=40, h=24)
    b.pad('A1', 5.0, 10.0, 'NET_A')
    b.pad('A2', 35.0, 10.0, 'NET_A')
    b.pad('B1', 5.0, 14.0, 'NET_B')
    b.pad('B2', 35.0, 14.0, 'NET_B')
    b.items.append(artwork)
    return b.text()


BOARD_RECT = ('  (gr_rect (start 15 8) (end 25 16) (stroke (width 0.1) (type solid)) '
              '(fill solid) (layer "F.Mask") (uuid "mask-rect"))')
# The same opening drawn inside a pad-less (logo) footprint placed at the box
# centre and rotated 90 degrees: local 8 x 10 becomes board 10 x 8.
LOGO = ('  (footprint "synth:LOGO" (layer "F.Cu") (uuid "logo-fp") (at 20 12 90)\n'
        '    (property "Reference" "G1" (at 0 0 0) (layer "F.SilkS") (uuid "logo-ref")\n'
        '      (effects (font (size 0.5 0.5) (thickness 0.1))))\n'
        '    (fp_poly (pts (xy -4 -5) (xy 4 -5) (xy 4 5) (xy -4 5)) '
        '(stroke (width 0) (type solid)) (fill solid) (layer "F.Mask") (uuid "logo-poly"))\n'
        '  )')


def _seg_hits_box(x1, y1, x2, y2, half, box):
    """Does a segment widened by `half` overlap the axis-aligned box?"""
    bx1, by1, bx2, by2 = box[0] - half, box[1] - half, box[2] + half, box[3] + half
    # Liang-Barsky clip of the centre line against the inflated box.
    t0, t1 = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for p, q in ((-dx, x1 - bx1), (dx, bx2 - x1), (-dy, y1 - by1), (dy, by2 - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _route(text):
    tmp = tempfile.mkdtemp(prefix='maskart_')
    inp = os.path.join(tmp, 'in.kicad_pcb')
    out = os.path.join(tmp, 'out.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as f:
        f.write(text)
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0')
    r = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(ROOT, 'py_router', 'route.py'),
                        inp, out, '--nets', 'NET_A', 'NET_B', '--escalation', 'off',
                        '--no-fix-drc-settings'],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    assert os.path.exists(out), 'route.py wrote no output (rc %d):\n%s' % (
        r.returncode, (r.stdout + r.stderr)[-2000:])
    return parse_kicad_pcb(out)


def _check(text, what):
    pcb = _route(text)
    names = {n.net_id: n.name for n in pcb.nets.values()}
    routed = {names.get(s.net_id) for s in pcb.segments}
    assert {'NET_A', 'NET_B'} <= routed, '%s: nets not routed: %s' % (what, routed)
    bad = []
    for s in pcb.segments:
        if s.layer == 'F.Cu' and _seg_hits_box(s.start_x, s.start_y, s.end_x, s.end_y,
                                               s.width / 2.0, BOX):
            bad.append('track %s (%.3f,%.3f)-(%.3f,%.3f)' % (
                names.get(s.net_id), s.start_x, s.start_y, s.end_x, s.end_y))
    for v in pcb.vias:
        if _seg_hits_box(v.x, v.y, v.x, v.y, v.size / 2.0, BOX):
            bad.append('via %s (%.3f,%.3f)' % (names.get(v.net_id), v.x, v.y))
    assert not bad, '%s: F.Cu copper under F.Mask artwork: %s' % (what, '; '.join(bad))


def test_board_mask_rect_keeps_copper_out():
    _check(_board(BOARD_RECT), 'gr_rect on F.Mask')


def test_logo_footprint_mask_poly_keeps_copper_out():
    _check(_board(LOGO), 'pad-less footprint fp_poly on F.Mask')


# A mask opening around a netted pad is that pad's own opening (mask-defined
# pad): it must not wall the pad off.
PAD_OPENING = ('  (gr_rect (start 4 9) (end 6 11) (stroke (width 0.1) (type solid)) '
               '(fill solid) (layer "F.Mask") (uuid "pad-open"))')


def test_pad_own_mask_opening_stays_routable():
    pcb = _route(_board(PAD_OPENING))
    names = {n.net_id: n.name for n in pcb.nets.values()}
    routed = {names.get(s.net_id) for s in pcb.segments}
    assert 'NET_A' in routed, 'pad inside its own mask opening not routed: %s' % routed


if __name__ == '__main__':
    test_board_mask_rect_keeps_copper_out()
    test_logo_footprint_mask_poly_keeps_copper_out()
    test_pad_own_mask_opening_stays_routable()
    print('PASS')
