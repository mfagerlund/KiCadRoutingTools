#!/usr/bin/env python3
"""The stub-debris trim must not leave a GHOST segment (#805, segment half).

The trim drops a net's unused stub branches from `pcb_data.segments` the
moment its route commits, freeing their obstacle cells for the next net. The
output writer emits segments from TWO sources: each result's `new_segments`
and the run's `all_swap_segments` (layer-swap reuse connectors, global-plan
escape fanout).
A stub that lives in `all_swap_segments` and is dropped from `pcb_data` alone
becomes a ghost: absent from the board model, present in the written file.

Measured on ecp5-dc-scm (stubs placed by an escape pre-pass): ROT_QSPI_DQ2
routed without its stub, the trim freed it, ROT_QSPI_DQ3 routed 0.2 mm from the
stub end that was still written (2 shorting_items, 2 clearance).

The test asserts the invariant, with its own negative control: without the
swap list handed over the ghost must appear.

    python3 tests/test_NNNN_trim_ghost_segments.py
"""
import os
import sys

RUN_ALL_TIMEOUT = 120
#: milliseconds; runs no chain and starts no child process.
RUN_ALL_FAST_OK = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))

FAILURES = []


def check(name, ok, detail=''):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


class _Cfg:
    clearance = 0.2
    track_width = 0.2
    via_size = 0.5
    via_drill = 0.25
    verbose = False
    debug_lines = None
    _keep_input_copper = False

    def obstacle_clearance(self, net_id):
        return 0.2


def _scenario():
    """A routed net with an unused stub that also sits in the swap list."""
    from kicad_parser import parse_kicad_pcb, Segment
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    nid = next(n for n, p in pcb.pads_by_net.items() if n and len(p) >= 2)
    pads = pcb.pads_by_net[nid]
    pcb.segments = [s for s in pcb.segments if s.net_id != nid]
    pcb.vias = [v for v in pcb.vias if v.net_id != nid]
    ax, ay = pads[0].global_x, pads[0].global_y
    bx, by = pads[1].global_x, pads[1].global_y
    mk = lambda x1, y1, x2, y2: Segment(  # noqa: E731
        start_x=x1, start_y=y1, end_x=x2, end_y=y2,
        width=0.2, layer='F.Cu', net_id=nid)
    main = mk(ax, ay, bx, by)
    stub = mk(bx, by, bx + 0.6, by + 0.6)       # dead-ended, unused by the route
    pcb.segments += [main, stub]
    swap = [stub]                               # the writer's OTHER segment source
    result = {'new_segments': [main], 'new_vias': []}
    return pcb, nid, result, swap, stub


def test_negative_control_the_ghost_is_reproducible():
    from pcb_modification import trim_net_stub_debris
    pcb, nid, result, swap, stub = _scenario()
    s_rm, _v = trim_net_stub_debris(pcb, nid, result, _Cfg())   # swap list omitted
    gone_from_model = not any(s is stub for s in pcb.segments)
    still_writable = any(s is stub for s in swap)
    check('the trim removes the unused stub at all', s_rm >= 1, f'{s_rm} segment(s)')
    check('CONTROL: omitting the swap list reproduces the ghost',
          gone_from_model and still_writable,
          f'gone_from_model={gone_from_model} still_writable={still_writable}')


def test_the_swap_list_is_stripped_in_place():
    from pcb_modification import trim_net_stub_debris
    pcb, nid, result, swap, stub = _scenario()
    held_by_writer = swap
    trim_net_stub_debris(pcb, nid, result, _Cfg(), swap_segments=swap)
    gone_from_model = not any(s is stub for s in pcb.segments)
    check('the stub leaves pcb_data.segments', gone_from_model)
    check('the stub leaves the writer\'s own list object',
          not any(s is stub for s in held_by_writer))


def test_the_state_carries_the_writer_list():
    """The SE loop reads state.all_swap_segments: the factory must alias it."""
    from routing_state import create_routing_state
    held = []
    st = create_routing_state(pcb_data=None, config=None, all_net_ids_to_route=[],
                              base_obstacles=None, diff_pair_base_obstacles=None,
                              diff_pair_extra_clearance=0.0, gnd_net_id=None,
                              all_unrouted_net_ids=[], total_routes=0,
                              all_swap_segments=held)
    check('state.all_swap_segments IS the caller\'s list', st.all_swap_segments is held)


def main():
    print('--- the stub-debris trim must not leave ghost segments')
    test_negative_control_the_ghost_is_reproducible()
    test_the_swap_list_is_stripped_in_place()
    test_the_state_carries_the_writer_list()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print('\ntest_NNNN_trim_ghost_segments: all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
