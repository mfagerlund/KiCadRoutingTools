"""A mask opening that meets a netted pad is cut out around the pad (OWN_MARGIN)
so the pad's own net can leave it. That band was open to EVERY net: another
net's track through it lies bare in the same opening as the pad, and KiCad
reports solder_mask_bridge. The band must stay exempt for the pad's net only:
the net-filtered keep-out guard carries one region per band binding every
other net."""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from synth import RoutingBoard  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from mask_artwork import _point_in  # noqa: E402

OPENING = ('  (gr_rect (start 15 8) (end 25 16) (stroke (width 0) (type solid)) '
           '(fill solid) (layer "F.Mask") (uuid "open"))')


def _pcb(mask_keepouts=None):
    b = RoutingBoard(w=40, h=24)
    b.pad('C1', 18.0, 12.0, 'NET_C', sx=1.0, sy=1.0)   # in the opening
    b.pad('C2', 38.0, 2.0, 'NET_C')
    b.pad('B1', 5.0, 14.0, 'NET_B')
    b.pad('B2', 35.0, 14.0, 'NET_B')
    b.items.append(OPENING)
    tmp = tempfile.mkdtemp(prefix='maskband_')
    try:
        path = os.path.join(tmp, 'b.kicad_pcb')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(b.text())
        pcb = parse_kicad_pcb(path)
        pcb.source_path = path
        if mask_keepouts is not None:
            pcb.board_info.mask_keepouts = mask_keepouts
        from net_keepouts import derive_mating_keepouts
        return derive_mating_keepouts(pcb)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _regions_at(kos, pt):
    return [k for k in kos if _point_in(pt, k['polygon'])
            and not any(_point_in(pt, h) for h in k.get('holes') or ())]


def test_band_binds_other_nets_not_the_owner():
    from net_keepouts import binds
    # (18.8, 12): 0.3 mm past the pad edge, inside the cut-out band
    at = _regions_at(_pcb(), (18.8, 12.0))
    assert at, 'no net-filtered region over the pad band in the opening'
    assert all('F.Cu' in k['layers'] and not k['tracks_allowed']
               and not k['vias_allowed'] for k in at), at
    assert any(binds(k, 'NET_B') for k in at), 'other net may enter the band'
    assert not any(binds(k, 'NET_C') for k in at), 'pad net walled off its own band'


def test_band_outside_the_opening_is_no_region():
    # the band is the opening's part only: (17.2, 12) is the band, (16.0, 12)
    # is outside the pad's grown copper
    kos = _pcb()
    assert _regions_at(kos, (17.2, 12.0))
    assert not _regions_at(kos, (12.0, 12.0))


def test_opt_out_derives_no_band():
    assert not [k for k in _pcb(mask_keepouts=False) if k.get('mask_band')]


def test_band_is_not_stamped_for_a_set_its_owner_routes_in():
    # a diff pair routes P and N on one centreline map: P's band binds N, and
    # stamping it for the pair walled P's pad off (27 pair nets open on a
    # board with pair pads in a mask opening). A band is stamped only for a
    # set none of whose nets owns it; a lone other net still gets it.
    b = RoutingBoard(w=40, h=24)
    b.pad('P1', 18.0, 12.0, 'NET_P', sx=1.0, sy=1.0)
    b.pad('P2', 38.0, 2.0, 'NET_P')
    b.pad('N1', 5.0, 14.0, 'NET_N')
    b.pad('N2', 35.0, 14.0, 'NET_N')
    b.items.append(OPENING)
    tmp = tempfile.mkdtemp(prefix='maskband_')
    try:
        path = os.path.join(tmp, 'b.kicad_pcb')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(b.text())
        pcb = parse_kicad_pcb(path)
        pcb.source_path = path
        import net_keepouts
        net_keepouts.attach(pcb, net_keepouts.derive_mating_keepouts(pcb))
        ids = {n.name: nid for nid, n in pcb.nets.items()}
        band = [i for i, k in enumerate(pcb._net_keepouts) if k.get('mask_band')]
        assert band
        assert set(band) <= set(net_keepouts._binding(pcb, [ids['NET_N']]))
        assert not set(band) & set(net_keepouts._binding(pcb, [ids['NET_P'], ids['NET_N']]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
