#!/usr/bin/env python3
"""Tests sin framework para la lógica de de-duplicación de alertas de error.

Ejecutar:  python3 test_monitor.py
"""

import asyncio

from monitor import (
    _outage_transition, _goto_resilient, NAV_ATTEMPTS,
    _build_alert_messages, ALERT_BATCH_SIZE,
    _fmt_duration, _format_sensor,
)

TELEGRAM_MAX_CHARS = 4096


def _fake_red(n: int) -> dict:
    """n sensores en rojo con el formato real del Score Card."""
    return {
        f"Sensor {i:02d}": {"text": (
            f"Sala Refrigerada Nº{i:02d} Planta Norte -18.5 ºC "
            f"Max: -15.0ºC Min: -22.0ºC 11 Agosto 2026 14:32:05 "
            f"T. Fuera Rango: 03:12:44"
        )}
        for i in range(1, n + 1)
    }


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


def test_alerta_una_tanda_sin_numerar():
    # Pocos sensores → un solo mensaje, sin "(parte 1/1)".
    msgs = _build_alert_messages(_fake_red(3), "14:32  11/08/2026")
    assert len(msgs) == 1
    tanda, texto = msgs[0]
    assert len(tanda) == 3
    assert "parte" not in texto
    assert "3 sensores fuera de rango" in texto


def test_alerta_divide_en_tandas_de_8():
    # 20 sensores → 3 mensajes (8 + 8 + 4), sin perder ni repetir ninguno.
    red = _fake_red(20)
    msgs = _build_alert_messages(red, "14:32  11/08/2026")
    assert len(msgs) == 3
    tamanos = [len(t) for t, _ in msgs]
    assert tamanos == [ALERT_BATCH_SIZE, ALERT_BATCH_SIZE, 4]
    enviados = [name for tanda, _ in msgs for name in tanda]
    assert enviados == list(red)  # todos, en orden, sin duplicados
    assert "(parte 1/3)" in msgs[0][1]
    assert "(parte 3/3)" in msgs[2][1]
    # El total va en todos los mensajes, no el tamaño de la tanda.
    assert all("20 sensores fuera de rango" in texto for _, texto in msgs)


def test_ningun_mensaje_excede_el_limite_de_telegram():
    # La razón de ser de las tandas: Telegram rechaza >4096 caracteres.
    for n in (1, 8, 9, 20, 50):
        for _, texto in _build_alert_messages(_fake_red(n), "14:32  11/08/2026"):
            assert len(texto) < TELEGRAM_MAX_CHARS, f"mensaje muy largo con {n} sensores"


def test_sin_sensores_no_genera_mensajes():
    assert _build_alert_messages({}, "14:32  11/08/2026") == []


def test_duracion_con_dias_horas_y_minutos():
    # Formatos reales del Score Card, incluido el encoding roto del sitio.
    assert _fmt_duration("02:09:44") == "2h 09min"
    assert _fmt_duration("00:45:12") == "45min"
    assert _fmt_duration("2 Dï¿½as 17 Hrs 33 Min") == "2d 17h 33min"
    assert _fmt_duration("1 Dï¿½a 16 Hrs 8 Min") == "1d 16h 08min"
    assert _fmt_duration("3 Días 12 Min") == "3d 12min"


def test_sensor_con_dias_y_horas_se_formatea_bonito():
    # Caso real del 17-ago-2026 que llegaba como texto crudo (sin formato):
    # la duración "N Días N Hrs N Min" no calzaba con el regex.
    raw = ("San Antonio Bodega - Anden - Fresco 4.3 ºC Max: 2ºC Min: -6ºC "
           "17 Agos 2026 09:20:37 T. Fuera: 2 Dï¿½as 17 Hrs 33 Min")
    out = _format_sensor(raw)
    assert not out.startswith("•"), f"cayó al formato crudo: {out[:80]}"
    assert "<b>San Antonio Bodega - Anden - Fresco</b>" in out
    assert "2d 17h 33min" in out
    assert "4.3°C" in out
    # Y el formato de siempre (HH:MM:SS) sigue funcionando igual.
    raw2 = ("Coquimbo Bodega - Anden S1 2.4 ºC Max: 2ºC Min: -6ºC "
            "17 Agos 2026 00:00:00 T. Fuera Rango: 02:09:44")
    out2 = _format_sensor(raw2)
    assert not out2.startswith("•")
    assert "2h 09min" in out2


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
