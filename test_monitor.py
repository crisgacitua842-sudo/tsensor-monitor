#!/usr/bin/env python3
"""Tests sin framework para la lógica de de-duplicación de alertas de error.

Ejecutar:  python3 test_monitor.py
"""

from monitor import _outage_transition


def test_primera_falla_alerta():
    # El sitio estaba OK y ahora falló → alertar ERROR, marcar caído.
    send_error, send_recovery, new_down = _outage_transition(prev_down=False, run_ok=False)
    assert send_error is True
    assert send_recovery is False
    assert new_down is True


def test_falla_repetida_no_realerta():
    # El sitio ya estaba marcado caído y vuelve a fallar → NO re-alertar.
    send_error, send_recovery, new_down = _outage_transition(prev_down=True, run_ok=False)
    assert send_error is False
    assert send_recovery is False
    assert new_down is True


def test_recuperacion_alerta():
    # Estaba caído y ahora la corrida fue OK → alertar recuperación, limpiar flag.
    send_error, send_recovery, new_down = _outage_transition(prev_down=True, run_ok=True)
    assert send_error is False
    assert send_recovery is True
    assert new_down is False


def test_normal_sin_alertas():
    # Estaba OK y sigue OK → no alertar nada.
    send_error, send_recovery, new_down = _outage_transition(prev_down=False, run_ok=True)
    assert send_error is False
    assert send_recovery is False
    assert new_down is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    raise SystemExit(1 if failed else 0)
