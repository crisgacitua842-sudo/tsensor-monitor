#!/usr/bin/env python3
"""Cortafuegos de efectos externos para la marcha blanca en Railway.

Durante la marcha blanca, Railway y GitHub Actions revisan T-Sensor EN PARALELO.
Si ambos pudieran avisar, el usuario recibiría alertas duplicadas; si ambos
escribieran state.json, se pisarían el estado (la API de GitHub protege las
escrituras con SHA, pero no protege la decisión de "este sensor es nuevo").

Por eso el proceso de Railway corre en modo SOMBRA: hace el scraping real pero
tiene prohibido tocar el mundo exterior. Solo registra en el log lo que HABRÍA
hecho.

Diseño (dos capas, a propósito):

1. Se reemplazan las tres funciones-embudo de monitor.py (send_telegram,
   write_state, ping_healthcheck). Es el camino limpio: devuelven lo que el
   código de arriba espera, así que la lógica sigue igual y el log queda legible.

2. Se intercepta aiohttp.ClientSession._request como red de seguridad. Si
   alguien agrega mañana un efecto nuevo por fuera de esas tres funciones, se
   bloquea igual y grita en el log. Nunca debería dispararse: es el cinturón
   además de los tirantes.

El valor por defecto es "shadow" a propósito: si alguien se equivoca al escribir
la variable o se pierde en un redeploy, el resultado es dejar de avisar (lo
detecta healthchecks.io en minutos) y NUNCA duplicar avisos (que solo lo detecta
el usuario, molesto). Fallar hacia el lado silencioso es lo correcto acá.
"""

import os

import aiohttp

MODE_ENV = "EFFECTS_MODE"
SHADOW = "shadow"
LIVE = "live"


class EffectBlocked(RuntimeError):
    """Un efecto externo intentó salir en modo sombra por una vía no prevista."""


def current_mode() -> str:
    """Modo actual. Cualquier valor que no sea exactamente 'live' es sombra."""
    return LIVE if os.environ.get(MODE_ENV, SHADOW).strip().lower() == LIVE else SHADOW


def is_shadow() -> bool:
    return current_mode() == SHADOW


STATE_RAW_URL = (
    "https://raw.githubusercontent.com/crisgacitua842-sudo/tsensor-monitor/main/state.json"
)


def _request_allowed(method: str, url: str) -> bool:
    """En sombra solo se permite LEER el estado por la vía pública de GitHub.

    Leer es inofensivo y hace que la comparación sea realista: el proceso sombra
    ve el mismo estado que ve GitHub Actions, así que su log dice exactamente lo
    que habría hecho de estar al mando. Se usa raw.githubusercontent (el repo es
    público) en vez de la API: no necesita ningún token, así que el proceso es
    físicamente incapaz de escribir aunque quisiera. Todo lo demás (Telegram,
    healthchecks, cualquier escritura) queda bloqueado.
    """
    if method.upper() != "GET":
        return False
    return url.startswith("https://raw.githubusercontent.com/")


def install(monitor_module, log=print) -> str:
    """Aplica el cortafuegos sobre el módulo monitor. Retorna el modo activo."""
    mode = current_mode()
    if mode == LIVE:
        log("  [efectos] MODO EN VIVO — Telegram, estado y healthchecks ACTIVOS.")
        return mode

    log("  [efectos] MODO SOMBRA — no se enviará nada a Telegram, no se escribirá "
        "state.json y no se pingueará healthchecks.")

    async def _telegram_sombra(text: str) -> bool:
        # Se devuelve True (como si se hubiera entregado) para que monitor.py siga
        # su curso normal: marca los sensores como alertados EN MEMORIA y no manda
        # /fail a healthchecks. Como el estado tampoco se persiste, ese marcado se
        # pierde al terminar el ciclo, que es justo lo que queremos en sombra.
        primera = text.splitlines()[0] if text else ""
        log(f"  [sombra] SE HABRÍA ENVIADO a Telegram ({len(text)} caracteres): {primera}")
        for linea in text.splitlines():
            log(f"  [sombra] │ {linea}")
        return True

    async def _write_state_sombra(state: dict) -> bool:
        alertados = list((state or {}).get("alerted", {}))
        log(f"  [sombra] SE HABRÍA GUARDADO state.json — sensores alertados: {alertados or 'ninguno'}")
        return True

    async def _ping_sombra(suffix: str = "") -> None:
        log(f"  [sombra] SE HABRÍA PINGUEADO healthchecks{suffix or ' (OK)'}")

    async def _read_state_sombra() -> dict:
        """Lee el estado REAL que dejó GitHub Actions, sin credenciales.

        El repo es público, así que se puede leer por la URL cruda. Sin esto, la
        función original devuelve estado vacío cuando no hay token y el log
        sombra diría "habría alertado" por sensores que GitHub Actions ya avisó,
        que es justo la comparación que queremos hacer bien.
        """
        import json as _json

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    STATE_RAW_URL, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        log(f"  [sombra] No se pudo leer el estado real ({resp.status}); "
                            "se sigue con estado vacío.")
                        return {"alerted": {}}
                    estado = _json.loads(await resp.text())
        except Exception as e:
            log(f"  [sombra] No se pudo leer el estado real ({type(e).__name__}); "
                "se sigue con estado vacío.")
            return {"alerted": {}}

        log(f"  [sombra] Estado real leído de GitHub: "
            f"{len(estado.get('alerted', {}))} sensor(es) ya alertados.")
        return estado

    monitor_module.send_telegram = _telegram_sombra
    monitor_module.write_state = _write_state_sombra
    monitor_module.ping_healthcheck = _ping_sombra
    monitor_module.read_state = _read_state_sombra

    _install_network_firewall(log)
    return mode


_original_request = None


def _install_network_firewall(log=print) -> None:
    """Red de seguridad: bloquea cualquier petición HTTP no autorizada.

    monitor.py usa aiohttp SOLO para los tres efectos externos; Playwright no
    pasa por acá (habla con Chromium por su propio canal), así que la navegación
    a T-Sensor no se ve afectada.
    """
    global _original_request
    if _original_request is not None:
        return  # ya instalado

    _original_request = aiohttp.ClientSession._request

    async def _guarded(self, method, url, *args, **kwargs):
        if not is_shadow() or _request_allowed(str(method), str(url)):
            return await _original_request(self, method, url, *args, **kwargs)
        log(f"  [sombra] 🚫 BLOQUEADA petición no autorizada: {method} {str(url)[:120]}")
        raise EffectBlocked(f"modo sombra: {method} {url}")

    aiohttp.ClientSession._request = _guarded
