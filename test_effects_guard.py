#!/usr/bin/env python3
"""Tests del cortafuegos de efectos (modo sombra para la marcha blanca).

Es la pieza que garantiza que el proceso de Railway no pueda mandar alertas
duplicadas mientras convive con GitHub Actions, así que se prueba a conciencia.

Ejecutar:  python3 test_effects_guard.py
"""

import asyncio
import os
import types

import aiohttp

import effects_guard


def _con_modo(valor):
    """Fija EFFECTS_MODE (o lo borra si valor es None) y devuelve el anterior."""
    previo = os.environ.get("EFFECTS_MODE")
    if valor is None:
        os.environ.pop("EFFECTS_MODE", None)
    else:
        os.environ["EFFECTS_MODE"] = valor
    return previo


def test_por_defecto_es_sombra():
    # Lo más importante del diseño: si la variable falta, NO se alerta.
    previo = _con_modo(None)
    try:
        assert effects_guard.current_mode() == "shadow"
        assert effects_guard.is_shadow() is True
    finally:
        _con_modo(previo)


def test_solo_la_palabra_exacta_enciende_los_efectos():
    previo = _con_modo(None)
    try:
        for encendido in ("live", "LIVE", " Live "):
            _con_modo(encendido)
            assert effects_guard.current_mode() == "live", encendido
        # Cualquier cosa rara (typo, vacío, valor inesperado) cae en sombra.
        for apagado in ("", "liv", "true", "1", "shadow", "sombra", "yes"):
            _con_modo(apagado)
            assert effects_guard.current_mode() == "shadow", apagado
    finally:
        _con_modo(previo)


def test_lista_blanca_solo_deja_pasar_lectura_de_estado():
    permitido = effects_guard._request_allowed
    # Único permitido: leer el state.json público del repo.
    assert permitido("GET", effects_guard.STATE_RAW_URL) is True
    assert permitido("get", effects_guard.STATE_RAW_URL) is True
    # Todo lo que produce un efecto queda fuera.
    assert permitido("POST", "https://api.telegram.org/bot123/sendMessage") is False
    assert permitido("GET", "https://hc-ping.com/d8f6c62d-a1e4-4b14-8d1b-74fee36859af") is False
    assert permitido("GET", "https://hc-ping.com/d8f6c62d/fail") is False
    assert permitido("PUT", "https://api.github.com/repos/x/y/contents/state.json") is False
    assert permitido("GET", "https://api.github.com/repos/x/y/contents/state.json") is False
    # Ni siquiera un POST al host permitido (escribir es siempre no).
    assert permitido("POST", effects_guard.STATE_RAW_URL) is False


def test_install_reemplaza_las_cuatro_funciones_de_efecto():
    previo = _con_modo("shadow")
    try:
        falso = types.SimpleNamespace(
            send_telegram="original", write_state="original",
            ping_healthcheck="original", read_state="original",
        )
        modo = effects_guard.install(falso, log=lambda _m: None)
        assert modo == "shadow"
        for nombre in ("send_telegram", "write_state", "ping_healthcheck", "read_state"):
            assert getattr(falso, nombre) != "original", f"{nombre} quedó sin reemplazar"
    finally:
        _con_modo(previo)


def test_en_vivo_no_toca_nada():
    previo = _con_modo("live")
    try:
        falso = types.SimpleNamespace(
            send_telegram="original", write_state="original",
            ping_healthcheck="original", read_state="original",
        )
        modo = effects_guard.install(falso, log=lambda _m: None)
        assert modo == "live"
        for nombre in ("send_telegram", "write_state", "ping_healthcheck", "read_state"):
            assert getattr(falso, nombre) == "original", f"{nombre} fue alterada en modo vivo"
    finally:
        _con_modo(previo)


def test_telegram_sombra_no_manda_pero_deja_seguir():
    previo = _con_modo("shadow")
    try:
        falso = types.SimpleNamespace(
            send_telegram=None, write_state=None, ping_healthcheck=None, read_state=None,
        )
        registro = []
        effects_guard.install(falso, log=registro.append)
        # Devuelve True para que monitor.py siga su curso normal (y no mande /fail),
        # pero el mensaje solo va al log.
        assert asyncio.run(falso.send_telegram("🚨 ALERTA\nSensor X")) is True
        assert any("SE HABRÍA ENVIADO" in m for m in registro)
        assert any("Sensor X" in m for m in registro)
    finally:
        _con_modo(previo)


def test_cortafuegos_bloquea_peticiones_no_autorizadas():
    """La red de seguridad: aunque alguien llame a aiohttp por fuera, no sale."""
    previo = _con_modo("shadow")
    try:
        falso = types.SimpleNamespace(
            send_telegram=None, write_state=None, ping_healthcheck=None, read_state=None,
        )
        effects_guard.install(falso, log=lambda _m: None)

        async def intentar_enviar():
            async with aiohttp.ClientSession() as s:
                async with s.post("https://api.telegram.org/bot123/sendMessage",
                                  json={"text": "hola"}):
                    return "SALIÓ"

        try:
            asyncio.run(intentar_enviar())
            bloqueado = False
        except effects_guard.EffectBlocked:
            bloqueado = True
        assert bloqueado, "¡el cortafuegos dejó salir un POST a Telegram!"
    finally:
        _con_modo(previo)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fallidos = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            fallidos += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - fallidos}/{len(tests)} tests OK")
    raise SystemExit(1 if fallidos else 0)
