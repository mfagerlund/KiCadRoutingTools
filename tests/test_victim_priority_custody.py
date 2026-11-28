#!/usr/bin/env python3
"""#622 victim-priority restore must not give an input-connected net's channel away.

A reconcile sub-run routes the outer run's casualties against the outer run's
output. There, copper the OUTER run routed counts as pre-existing: when a
casualty (a net that was connected on the outer input and broken by the outer
run) rips such a net and that net's reroute fails, the endgame sweep used to
rip the casualty back so the outer run's net restores whole. The board then
ships the input-connected net broken to rescue a net that was open on input.

Contract under test:
  * victim_may_displace refuses exactly that swap and allows every other;
  * the sweep consults it before ripping squatters;
  * batch_route takes input_custody (never blanket-forwarded to a sub-run),
    stamps it on the config, and the outer run hands its retried pre-existing
    rip victims to the reconcile sub-run.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))


def test_custody_squatter_is_not_displaced_by_a_net_without_custody():
    from reroute_loop import victim_may_displace
    assert not victim_may_displace({'CS'}, 'SIG_B', ['CS'])
    assert not victim_may_displace({'CS'}, 'SIG_B', ['OTHER', 'CS'])


def test_other_swaps_unchanged():
    from reroute_loop import victim_may_displace
    assert victim_may_displace(set(), 'SIG_B', ['CS'])          # outer run / no custody
    assert victim_may_displace(None, 'SIG_B', ['CS'])
    assert victim_may_displace({'CS'}, 'SIG_B', ['OTHER'])      # squatter without custody
    assert victim_may_displace({'CS', 'SIG_B'}, 'SIG_B', ['CS'])  # both had custody: #622 as before


def test_sweep_consults_custody_before_ripping():
    import reroute_loop
    src = inspect.getsource(reroute_loop)
    i_check = src.index('victim_may_displace(\n')
    i_rip = src.index('rip_up_net(\n                    _o, pcb_data')
    assert i_check < i_rip


def test_batch_route_carries_input_custody_to_the_subrun_only():
    # source check: importing route.py needs the compiled router module
    path = os.path.join(os.path.dirname(__file__), '..', 'py_router', 'route.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    sig = src.index('def batch_route(')
    assert src.index('input_custody: Optional[List[str]] = None', sig) > sig
    assert "config.input_custody = frozenset(input_custody or ())" in src
    pop = src.index("'input_custody'):" + chr(10) + "        _reconcile_kwargs.pop(_k, None)")
    hand = src.index("_rk['input_custody'] = _custody9")
    assert sig < pop < hand
