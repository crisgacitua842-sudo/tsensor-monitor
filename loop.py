#!/usr/bin/env python3
"""Proceso always-on del monitor T-Sensor (Railway).

En GitHub Actions cada revisión es un proceso que nace, revisa una vez y muere.
Acá el proceso vive permanentemente y se auto-agenda cada CHECK_INTERVAL_MIN
minutos. Eso elimina los dos eslabones que venían fallando: los disparos de
cron-job.org que se saltaban y la preparación lenta del runner (apt-get) que
atrasaba el ping y pintaba caídas falsas en healthchecks.io.

Modo sombra: ver effects_guard.py. Por defecto NO toca el mundo exterior.

Modo sondeo: si faltan las credenciales de T-Sensor, en vez de fallar en bucle
hace un sondeo de conectividad contra la página de login y reporta si la IP de
Railway puede siquiera hablar con T-Sensor. Sirve para medir, antes de tener
credenciales, el riesgo que hundió al bot en julio-2026 (el reCAPTCHA de Google
dejó de resolver desde IPs de datacenter).
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from zoneinfo import ZoneInfo

import effects_guard
import monitor

CHILE_TZ = ZoneInfo("America/Santiago")

CHECK_INTERVAL_MIN = float(os.environ.get("CHECK_INTERVAL_MIN", "10"))
CHECK_INTERVAL_SECS = CHECK_INTERVAL_MIN * 60

# Techo por ciclo. En GitHub Actions este papel lo cumplía `timeout-minutes: 15`
# del workflow — una red EXTERNA que en un proceso always-on no existe. Sin esto,
# un Chromium colgado (T-Sensor acepta la conexión y no responde: escenario ya
# documentado en _goto_resilient) congela el loop para siempre, en silencio.
# El peor caso teórico de monitor() con 3 intentos son ~11 min, más que el
# período de 10; cortamos antes para no desincronizar la cadencia.
CYCLE_TIMEOUT_SECS = float(os.environ.get("CYCLE_TIMEOUT_SECS", "480"))

# El peor caso de monitor.py con sus 3 intentos son ~11 min, más que el período
# de 10: un ciclo malo se comería el turno siguiente. Con 2 intentos el techo
# baja a ~7,4 min y entra holgado. GitHub Actions no se ve afectado: allá
# MAX_RETRIES se lee del propio módulo y sigue en 3.
MONITOR_MAX_RETRIES = int(os.environ.get("MONITOR_MAX_RETRIES", "2"))

_stopping = False


def log(msg: str) -> None:
    """Log con hora de Chile. flush inmediato: Railway lee stdout en vivo."""
    ts = datetime.now(CHILE_TZ).strftime("%d/%m %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _tiene_credenciales() -> bool:
    return bool(monitor.TSENSOR_USER and monitor.TSENSOR_PASS)


def _matar_chromium_huerfano() -> None:
    """Mata Chromium que haya quedado vivo tras cortar un ciclo colgado.

    Si cancelamos monitor() por timeout, Playwright puede dejar procesos vivos.
    En un proceso que corre semanas eso se acumula y consume la RAM que Railway
    factura, así que barremos después de cada corte.
    """
    try:
        r = subprocess.run(["pkill", "-f", "chrome|chromium"],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            log("  Se mataron procesos de Chromium huérfanos tras el corte.")
    except Exception as e:
        log(f"  No se pudo limpiar Chromium huérfano: {e}")


async def sondeo_conectividad() -> None:
    """Sin credenciales: mide si T-Sensor es alcanzable desde esta IP.

    Reporta las señales que distinguen los modos de falla conocidos:
      - la página no carga         → servidor de T-Sensor o red bloqueada
      - carga pero sin formulario  → nos están sirviendo otra cosa (bloqueo)
      - formulario oculto          → normal: el reCAPTCHA no resuelve desde
                                     datacenter, y por eso el login va por JS
      - formulario visible         → el reCAPTCHA sí resolvió desde esta IP
    """
    from playwright.async_api import async_playwright

    log("SONDEO de conectividad (sin credenciales cargadas)...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=monitor.CHROMIUM_ARGS)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            t0 = time.monotonic()
            resp = await page.goto("https://app.tsensor.online/",
                                   wait_until="commit", timeout=30_000)
            estado_http = resp.status if resp else "sin respuesta"
            log(f"  Respuesta HTTP: {estado_http} en {time.monotonic() - t0:.1f}s")

            await page.wait_for_timeout(12_000)
            señales = await page.evaluate("""() => {
                const f = document.querySelector('#acceso_login');
                const pw = document.querySelector('#UsuarioPsword');
                const btn = document.querySelector('#guardar');
                return {
                    campo_clave: pw ? 'existe' : 'NO EXISTE',
                    formulario: f ? getComputedStyle(f).display : 'NO EXISTE',
                    boton_bloqueado: btn ? btn.disabled : 'NO EXISTE',
                    recaptcha: typeof window.grecaptcha,
                    titulo: document.title,
                };
            }""")
            log(f"  Señales: {señales}")

            if señales["campo_clave"] == "existe":
                visible = señales["formulario"] != "none"
                detalle = ("el reCAPTCHA resolvió desde esta IP"
                           if visible else
                           "el reCAPTCHA no resolvió, igual que en GitHub — el login por JS lo evita")
                log(f"  ✅ T-Sensor ALCANZABLE desde esta IP: llegó el formulario de login ({detalle}).")
            else:
                log("  ❌ La página cargó pero SIN el formulario de login: posible bloqueo "
                    "a esta IP. Hay que revisar antes de migrar.")
        finally:
            await browser.close()


def _armar_vigilante(segundos: float):
    """Último recurso: si un ciclo se cuelga sin ceder, mata el proceso.

    asyncio.wait_for solo puede cancelar en un punto de espera; si algo se
    bloquea de verdad (Chromium zombi, una llamada nativa trabada), el loop
    quedaría congelado para siempre y en silencio. Este hilo aparte no depende
    del loop de asyncio: cuenta y, si nadie lo desarma a tiempo, corta por lo
    sano. Railway reinicia el contenedor (restartPolicyType: ALWAYS) y el
    monitor vuelve a la vida en segundos.
    """
    import threading

    def _matar():
        log(f"  🚨 El ciclo lleva {segundos / 60:.0f} min colgado sin ceder — "
            "se mata el proceso para que Railway lo reinicie.")
        sys.stdout.flush()
        os._exit(1)

    t = threading.Timer(segundos, _matar)
    t.daemon = True
    t.start()
    return t


async def un_ciclo() -> None:
    """Una revisión completa, con techo de tiempo y sin dejar morir el proceso."""
    vigilante = _armar_vigilante(CYCLE_TIMEOUT_SECS * 2)
    try:
        if _tiene_credenciales():
            await asyncio.wait_for(monitor.monitor(), timeout=CYCLE_TIMEOUT_SECS)
        else:
            await asyncio.wait_for(sondeo_conectividad(), timeout=CYCLE_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        log(f"  ⏱ Ciclo cortado por pasarse de {CYCLE_TIMEOUT_SECS / 60:.0f} min. "
            "Se reintenta en el próximo turno.")
        _matar_chromium_huerfano()
    except Exception as e:
        # monitor() relanza el error tras agotar sus 3 intentos. En GitHub Actions
        # eso pintaba la corrida en rojo y se acababa ahí; acá NO puede matar el
        # proceso: la política de reinicios de Railway tiene tope, así que una
        # caída larga de T-Sensor dejaría el servicio apagado para siempre. El
        # vigilante externo sigue siendo healthchecks.io, vía el /fail que
        # monitor.py ya manda solo.
        log(f"  ❌ Ciclo fallido: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        log("  El proceso sigue vivo; se reintenta en el próximo turno.")
    finally:
        vigilante.cancel()


def _pedir_parada(signum, _frame) -> None:
    global _stopping
    _stopping = True
    log(f"Señal {signal.Signals(signum).name} recibida — se cierra al terminar el ciclo.")


async def main() -> None:
    signal.signal(signal.SIGTERM, _pedir_parada)
    signal.signal(signal.SIGINT, _pedir_parada)

    modo = effects_guard.install(monitor, log=log)
    monitor.MAX_RETRIES = MONITOR_MAX_RETRIES
    log(f"Monitor T-Sensor always-on — cada {CHECK_INTERVAL_MIN:g} min — modo {modo.upper()}")
    if not _tiene_credenciales():
        log("⚠️ Faltan TSENSOR_USER / TSENSOR_PASS: se hará solo SONDEO de conectividad. "
            "Cárgalas en Railway para que empiece la marcha blanca de verdad.")

    # Se revisa de inmediato al arrancar: si no, un redeploy dejaría 10 minutos
    # ciegos y encima haría inútil mirar el log recién desplegado.
    siguiente = time.monotonic()

    while not _stopping:
        await un_ciclo()
        if _stopping:
            break

        # Cadencia con reloj monótono y vencimiento acumulado: que un ciclo tarde
        # 3 minutos no debe correr el horario de los siguientes. Y si un ciclo se
        # pasó de largo, se saltan los turnos perdidos en vez de dispararlos
        # todos juntos.
        ahora = time.monotonic()
        siguiente += CHECK_INTERVAL_SECS
        while siguiente <= ahora:
            siguiente += CHECK_INTERVAL_SECS

        espera = siguiente - ahora
        log(f"Próxima revisión en {espera / 60:.1f} min.")
        # Se duerme a pedacitos para reaccionar rápido a un SIGTERM de redeploy.
        while espera > 0 and not _stopping:
            tramo = min(5.0, espera)
            await asyncio.sleep(tramo)
            espera -= tramo

    log("Proceso detenido limpiamente.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)
