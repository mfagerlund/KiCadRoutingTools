"""Via rungs keep the board's copper-to-hole floor (--rung-hole-clearance / KICAD_HOLE_CLEARANCE)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
import fab_tiers as ft  # noqa: E402


def _ladder(hole, policy='fab'):
    prev_tier, prev_pol = ft.get_default_fab_tier(), ft.get_escalation_policy()
    try:
        ft.set_default_fab_tier('auto', {})
        ft.set_escalation_policy(policy, {})
        ft.set_hole_clearance(hole)
        return ft.escalation_rungs(4)
    finally:
        ft.set_hole_clearance(None)
        ft.set_default_fab_tier(*prev_tier)
        ft.set_escalation_policy(*prev_pol)


def test_rungs_raised_to_hole_clearance():
    plain = _ladder(None)
    assert any((r['via_diameter'] - r['via_drill']) / 2 + r['clearance'] < 0.25 - 1e-9 for r in plain), plain
    for r in _ladder(0.25):
        assert (r['via_diameter'] - r['via_drill']) / 2 + r['clearance'] >= 0.25 - 1e-9, r


def test_env_default(monkeypatch):
    monkeypatch.setenv('KICAD_HOLE_CLEARANCE', '0.25')
    assert ft._hole_clearance_default() == 0.25
    monkeypatch.setenv('KICAD_HOLE_CLEARANCE', 'x')
    assert ft._hole_clearance_default() is None
    monkeypatch.delenv('KICAD_HOLE_CLEARANCE')
    assert ft._hole_clearance_default() is None


if __name__ == '__main__':
    # run_all.py runs each test file as a script; without this block it defined its tests and ran none (#718).
    # pytest's monkeypatch fixture comes from the stdlib, so pytest stays optional.
    import inspect
    import os as _os
    import traceback

    class _MonkeyPatch:
        """The fixture methods these tests use (setattr, setenv, delenv), undone after each test."""

        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value):
            self._undo.append((target, name, getattr(target, name)))
            setattr(target, name, value)

        def setenv(self, name, value):
            self._undo.append((None, name, _os.environ.get(name)))
            _os.environ[name] = str(value)

        def delenv(self, name, raising=True):
            self._undo.append((None, name, _os.environ.get(name)))
            if raising:
                del _os.environ[name]
            else:
                _os.environ.pop(name, None)

        def undo(self):
            for target, name, value in reversed(self._undo):
                if target is not None:
                    setattr(target, name, value)
                elif value is None:
                    _os.environ.pop(name, None)
                else:
                    _os.environ[name] = value

    tests = [(n, f) for n, f in globals().items() if n.startswith('test_') and inspect.isfunction(f)]
    failed = []
    for name, fn in tests:
        mp = _MonkeyPatch()
        kwargs = {'monkeypatch': mp} if 'monkeypatch' in inspect.signature(fn).parameters else {}
        try:
            fn(**kwargs)
            print(f'  PASS  {name}')
        except Exception:                                  # noqa: BLE001
            failed.append(name)
            print(f'  FAIL  {name}')
            traceback.print_exc()
        finally:
            mp.undo()
    print(f'{len(tests) - len(failed)}/{len(tests)} passed')
    sys.exit(1 if failed or not tests else 0)
