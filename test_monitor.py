#!/usr/bin/env python3
"""Tests sin framework para la lógica de de-duplicación de alertas de error.

Ejecutar:  python3 test_monitor.py
"""

import asyncio

from monitor import _outage_transition, _goto_resilient, NAV_ATTEMPTS


class _FakePage:
    """Página falsa: las primeras `fail_gotos` navegaciones lanzan timeout."""

    def __init__(self, fail_gotos: int):
        self.fail_gotos = fail_gotos
        self.goto_calls = 0

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1
        if self.goto_calls <= self.fail_gotos:
            raise Exception("Page.goto: Timeout 20000ms exceeded.")

    async def wait_for_selector(self, selector, state=None, timeout=None):
        return True

    async def wait_for_timeout(self, ms):
        return None  # no dormimos de verdad en los tests


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


def test_nav_reintenta_y_logra_entrar():
    # Las 2 primeras navegaciones fallan, la 3ra entra → no debe lanzar.
    page = _FakePage(fail_gotos=NAV_ATTEMPTS - 1)
    asyncio.run(_goto_resilient(page, "http://x"))
    assert page.goto_calls == NAV_ATTEMPTS


def test_nav_se_rinde_si_todas_fallan():
    # Si todas las navegaciones fallan, propaga el error tras agotar intentos.
    page = _FakePage(fail_gotos=NAV_ATTEMPTS + 5)
    try:
        asyncio.run(_goto_resilient(page, "http://x"))
        raised = False
    except Exception:
        raised = True
    assert raised is True
    assert page.goto_calls == NAV_ATTEMPTS


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
