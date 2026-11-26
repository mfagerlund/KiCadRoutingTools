"""Partner-pad capsule of a hybrid leg encloses the pad's corner copper exactly (C10 rev A, U203.3/4)."""
import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
import diff_pair_routing as dpr  # noqa: E402


def pad(shape, sx, sy, rratio=0.0):
    return SimpleNamespace(global_x=0.0, global_y=0.0, size_x=sx, size_y=sy, shape=shape,
                           roundrect_rratio=rratio, layers=['F.Cu'], drill=0, net_id=1)


def width(p):
    (seg,) = dpr._pad_obstacle_segments(p, ['F.Cu', 'B.Cu'])
    return seg.width


def test_sharp_rect_keeps_sqrt2():
    assert math.isclose(width(pad('rect', 0.85, 0.3)), 0.3 * math.sqrt(2.0))


def test_oval_and_circle_are_exact():
    assert math.isclose(width(pad('oval', 0.85, 0.3)), 0.3)
    assert math.isclose(width(pad('circle', 0.5, 0.5)), 0.5)


def test_roundrect_encloses_its_corner_arc_only():
    # 0.5 mm pitch QFN pad: 0.30 short, rratio 0.25 -> corner radius 0.075
    w = width(pad('roundrect', 0.85, 0.3, 0.25))
    r = 0.075
    assert math.isclose(w / 2.0, math.sqrt(2.0) * (0.15 - r) + r)
    assert 0.3 < w < 0.3 * math.sqrt(2.0)
    # the farthest corner copper point lies on the capsule boundary, none outside
    half = 0.85 / 2 - 0.15
    cx, cy = 0.85 / 2 - r, 0.15 - r
    far = math.hypot(cx - half, cy) + r
    assert far <= w / 2.0 + 1e-12


def test_full_rratio_roundrect_is_a_stadium():
    assert math.isclose(width(pad('roundrect', 0.85, 0.3, 0.5)), 0.3)


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
