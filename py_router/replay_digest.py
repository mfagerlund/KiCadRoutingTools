"""Digest of the copper a pass leaves: pcb_data segments/vias and each result's new copper, geometry only."""
import hashlib


def _segs(segs):
    return sorted((round(s.start_x, 4), round(s.start_y, 4), round(s.end_x, 4), round(s.end_y, 4), s.layer,
                   round(s.width, 4), s.net_id) for s in segs or [] if not isinstance(s, dict))


def _vias(vias):
    return sorted((round(v.x, 4), round(v.y, 4), round(v.size, 4), round(v.drill, 4), tuple(v.layers), v.net_id)
                  for v in vias or [] if not isinstance(v, dict))


def copper_digest(pcb_data, results=None):
    h = hashlib.sha1()
    h.update(repr(_segs(pcb_data.segments)).encode())
    h.update(repr(_vias(pcb_data.vias)).encode())
    for r in results or []:
        h.update(repr(_segs(r.get('new_segments'))).encode())
        h.update(repr(_vias(r.get('new_vias'))).encode())
    return h.hexdigest()[:16]
