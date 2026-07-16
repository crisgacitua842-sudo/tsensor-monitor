#!/usr/bin/env python3
"""
T-Sensor temperature monitor
Sends Telegram alerts when any sensor turns red (out of range).
Only alerts once per incident — re-alerts if sensor recovers then goes red again.
"""

import asyncio
import base64
import html
import os
import re
import json
import traceback
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

CHILE_TZ = ZoneInfo("America/Santiago")
MAX_ALERT_AGE_DAYS = 7

_SENSOR_RE = re.compile(
    r'(.+?)\s+([-\d.]+)\s*[º°]C\s+'
    r'Max:\s*([-\d.]+)[º°]C\s+'
    r'Min:\s*([-\d.]+)[º°]C\s+'
    r'(\d{1,2}\s+\w+\s+\d{4})\s+'
    r'(\d{2}:\d{2}:\d{2})\s+'
    r'T\.\s*Fuera(?:\s+Rango)?:\s+'
    r'(\d{2}:\d{2}:\d{2}|\d+\s+D[íiï][^\s]*\s+\d+\s+Min(?:uto[s]?)?)',
    re.UNICODE | re.IGNORECASE,
)


def _fmt_duration(raw: str) -> str:
    day_m = re.match(r'(\d+)\s+D', raw, re.IGNORECASE)
    if day_m:
        mins_m = re.search(r'(\d+)\s+Min', raw, re.IGNORECASE)
        d = int(day_m.group(1))
        m = int(mins_m.group(1)) if mins_m else 0
        return f"{d}d {m:02d}min"
    parts = raw.split(':')
    h, m = int(parts[0]), int(parts[1])
    if h > 0:
        return f"{h}h {m:02d}min"
    return f"{m}min"


def _extract_name(raw: str) -> str:
    m = _SENSOR_RE.match(raw.strip())
    if m:
        return m.group(1).strip()
    # Fallback robusto: extraer solo el nombre antes de la temperatura
    clean = re.sub(r'\s+', ' ', raw.strip())
    name_m = re.match(r'^(.+?)\s+[-\d.]+\s*[º°]C', clean)
    if name_m:
        return name_m.group(1).strip()[:100]
    return clean.splitlines()[0].strip()[:100]


def _format_sensor(raw: str) -> str:
    m = _SENSOR_RE.match(raw.strip())
    if not m:
        return f"• {html.escape(raw)}"
    name, temp, max_t, min_t, fecha, hora, fuera = m.groups()
    duracion = _fmt_duration(fuera)
    hora_corta = hora[:5]  # HH:MM
    return (
        f"📍 <b>{html.escape(name.strip())}</b>\n"
        f"   🌡 Temp: <b>{html.escape(temp)}°C</b>   Rango: {html.escape(min_t)}°C → {html.escape(max_t)}°C\n"
        f"   ⏱ Fuera de rango: <b>{html.escape(duracion)}</b>\n"
        f"   🕐 Último dato: {html.escape(hora_corta)}  {html.escape(fecha)}"
    )


TSENSOR_URL      = os.environ.get("TSENSOR_URL", "https://app.tsensor.online/informes/menu/98")
TSENSOR_USER     = os.environ.get("TSENSOR_USER")
TSENSOR_PASS     = os.environ.get("TSENSOR_PASS")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "crisgacitua842-sudo/tsensor-monitor")
HC_PING_URL      = os.environ.get("HC_PING_URL", "https://hc-ping.com/d8f6c62d-a1e4-4b14-8d1b-74fee36859af")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

STATE_FILE = "state.json"
GH_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def _clean_stale_alerts(alerted: dict) -> dict:
    """Elimina sensores que llevan más de MAX_ALERT_AGE_DAYS en estado alertado."""
    now = datetime.now(CHILE_TZ)
    clean = {}
    for name, timestamp in alerted.items():
        try:
            age = (now - datetime.fromisoformat(timestamp)).days
            if age < MAX_ALERT_AGE_DAYS:
                clean[name] = timestamp
            else:
                print(f"  Estado expirado limpiado: {name} (alertado hace {age} días)")
        except Exception:
            pass  # timestamp inválido, descartar
    return clean


def _outage_transition(prev_down: bool, run_ok: bool) -> tuple[bool, bool, bool]:
    """Decide qué alertas de error mandar según el estado previo y el resultado del run.

    Retorna (enviar_error, enviar_recuperacion, nuevo_flag_caido):
      - enviar_error: True SOLO en la primera falla de una racha (evita spam).
      - enviar_recuperacion: True cuando el sitio vuelve tras una caída ya notificada.
      - nuevo_flag_caido: estado de caída a persistir en state.json.
    """
    if run_ok:
        return (False, prev_down, False)
    return (not prev_down, False, True)


async def read_state() -> dict:
    """Lee el estado de sensores alertados desde el repositorio GitHub."""
    if not GITHUB_TOKEN:
        return {"alerted": {}}
    async with aiohttp.ClientSession() as session:
        async with session.get(GH_API, headers=GH_HEADERS) as resp:
            if resp.status == 404:
                return {"alerted": {}}
            data = await resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            state = json.loads(content)
            state["_sha"] = data["sha"]
            return state


async def write_state(state: dict) -> bool:
    """Guarda el estado en GitHub. Reintenta con SHA fresco si hay conflicto.

    Retorna True si se guardó, False si todos los intentos fallaron.
    """
    if not GITHUB_TOKEN:
        return True

    sha = state.pop("_sha", None)
    to_persist = {
        "alerted": state.get("alerted", {}),
        "site_down": state.get("site_down", False),
    }
    if state.get("outage"):
        # Registro interno del último error del servidor (no se avisa al grupo).
        to_persist["outage"] = state["outage"]

    for attempt in range(1, 4):
        content = base64.b64encode(
            json.dumps(to_persist, indent=2, ensure_ascii=False).encode()
        ).decode()
        payload = {"message": "chore: update sensor state [skip ci]", "content": content}
        if sha:
            payload["sha"] = sha

        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    GH_API, headers=GH_HEADERS, json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (200, 201):
                        print(f"  Estado guardado en GitHub.")
                        return True
                    body = await resp.text()
                    print(f"  Error guardando estado (intento {attempt}/3): {resp.status} {body[:200]}")

                    if resp.status == 409:
                        try:
                            fresh = await read_state()
                            sha = fresh.get("_sha")
                            print("  SHA refrescado tras conflicto, reintentando...")
                        except Exception as e:
                            print(f"  No se pudo refrescar SHA: {e}")
                            return False
                    elif 400 <= resp.status < 500:
                        return False
                    else:
                        await asyncio.sleep(3)
        except Exception as e:
            print(f"  Excepción guardando estado (intento {attempt}/3): {e}")
            await asyncio.sleep(3)

    return False


async def ping_healthcheck(suffix: str = ""):
    """Notifica a healthchecks.io que el monitor corrió. /fail si hubo error."""
    if not HC_PING_URL:
        return
    url = HC_PING_URL.rstrip("/") + suffix
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                print(f"  Healthcheck ping{'  ' + suffix if suffix else ' OK'}: {resp.status}")
    except Exception as e:
        print(f"  Healthcheck ping falló: {e}")


async def send_telegram(text: str) -> bool:
    """Envía un mensaje a Telegram. Retorna True si el mensaje se entregó.

    Reintenta hasta 3 veces si hay un error transitorio (timeout, 5xx).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        print("  Alerta enviada por Telegram.")
                        return True
                    body = await resp.text()
                    print(f"  Error Telegram (intento {attempt}/3): {resp.status} {body[:300]}")
                    if 400 <= resp.status < 500:
                        return False  # error permanente (chat id malo, HTML inválido, etc.)
                    await asyncio.sleep(3)
        except Exception as e:
            print(f"  Excepción Telegram (intento {attempt}/3): {e}")
            await asyncio.sleep(3)

    return False


async def get_red_sensors_computed(page) -> list:
    return await page.evaluate("""() => {
        function isRedHex(hex) {
            const r = parseInt(hex.slice(1, 3), 16);
            const g = parseInt(hex.slice(3, 5), 16);
            const b = parseInt(hex.slice(5, 7), 16);
            return r > 150 && g < 100 && b < 100;
        }
        function hasRedBackground(el) {
            const style = el.getAttribute('style') || '';
            if (style.includes('redPulse')) return true;
            if (style.includes('background')) {
                const hexColors = style.match(/#[0-9a-fA-F]{6}/g) || [];
                if (hexColors.some(isRedHex)) return true;
            }
            const bg = window.getComputedStyle(el).backgroundColor;
            const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (m) {
                const [r, g, b] = [+m[1], +m[2], +m[3]];
                return r > 150 && g < 100 && b < 100;
            }
            return false;
        }
        function hasDegreeC(text) {
            return text.includes('\\u00baC') || text.includes('\\u00b0C');
        }
        const results = [];
        const seen = new Set();
        for (const el of document.querySelectorAll('*')) {
            if (!hasRedBackground(el)) continue;
            const text = (el.innerText || '').trim();
            if (!hasDegreeC(text)) continue;
            if (text.length > 400 || text.length < 10) continue;
            if (seen.has(text)) continue;
            seen.add(text);
            results.push({ text: text.replace(/\\s+/g, ' ') });
        }
        return results;
    }""")


NAV_ATTEMPTS = 3
NAV_TIMEOUT_MS = 20_000


async def _goto_resilient(page, url: str) -> None:
    """Navega reintentando con conexiones frescas.

    El servidor de T-Sensor a veces acepta la conexión pero no responde (se
    queda sin workers). En lugar de quemar 60s en una conexión muerta, probamos
    varias veces con timeout corto: una conexión nueva suele caer en un worker
    sano. Esperamos el campo de contraseña para confirmar que el login cargó de
    verdad, no solo que llegó una respuesta vacía.

    OJO: esperamos que el campo exista en el HTML (attached), no que se vea.
    El formulario viene con display:none y solo se muestra cuando el reCAPTCHA
    de Google responde; desde los runners de GitHub (IPs de datacenter) Google
    no responde nunca, así que esperar visibilidad deja al bot caído aunque el
    sitio esté sano (caída del 14-jul-2026).
    """
    last_err = None
    for i in range(1, NAV_ATTEMPTS + 1):
        try:
            await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_selector('input[type="password"]',
                                         state="attached", timeout=15_000)
            return
        except Exception as e:
            last_err = e
            print(f"  Navegación intento {i}/{NAV_ATTEMPTS} falló: {str(e).splitlines()[0][:90]}")
            if i < NAV_ATTEMPTS:
                await page.wait_for_timeout(3000)
    raise last_err


async def navigate_to_scorecard(page):
    """Login y navega hasta cargar el Score Card."""
    print("  Cargando página de login...")
    await _goto_resilient(page, TSENSOR_URL)
    await page.wait_for_timeout(2000)

    # Llenamos y enviamos el formulario por JavaScript, sin click en ENTRAR:
    # los campos pueden estar ocultos esperando al reCAPTCHA (que en GitHub
    # nunca llega) y el botón solo hace submit del formulario; el servidor no
    # exige el token del captcha para el login.
    await page.evaluate(
        """([usuario, clave]) => {
            const user = document.querySelector('#UsuarioLogin')
                || document.querySelector('form input[type="text"]');
            const pass = document.querySelector('#UsuarioPsword')
                || document.querySelector('input[type="password"]');
            user.value = usuario;
            pass.value = clave;
            (document.querySelector('#signin-form_id') || pass.form).submit();
        }""",
        [TSENSOR_USER, TSENSOR_PASS],
    )

    await page.wait_for_load_state("networkidle", timeout=20_000)
    await page.wait_for_timeout(2000)

    print("  Haciendo click en TELEMETRÍA...")
    await page.click('a:has-text("TELEMETRÍA"), a:has-text("Telemetría")')
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await page.wait_for_timeout(3000)

    if DEBUG:
        await page.screenshot(path="debug_login.png")

    mostrar = await page.query_selector(
        'a:has-text("Mostrar Todos"), input[value="Mostrar Todos"], button:has-text("Mostrar Todos")'
    )
    if mostrar:
        await mostrar.click()
        print("  Click en Mostrar Todos (AJAX cascadeante), esperando carga...")
    else:
        print("  ADVERTENCIA: No se encontró Mostrar Todos")
    await page.wait_for_timeout(8000)

    if DEBUG:
        await page.screenshot(path="debug_after_mostrar.png")

    await page.evaluate("""() => {
        const sel = document.querySelector('#InformeId');
        if (!sel) return;
        sel.disabled = false;
        for (const opt of sel.options) {
            if (opt.text.trim() === 'Score Card') {
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                sel.dispatchEvent(new Event('input',  { bubbles: true }));
                break;
            }
        }
    }""")
    print("  Score Card seleccionado")

    try:
        await page.wait_for_selector(
            'button#ver_online2:not([disabled]), button[name="boton"]:not([disabled])',
            timeout=10_000
        )
        print("  Botón Ver Online habilitado")
    except Exception:
        print("  Ver Online sigue deshabilitado — habilitando vía JS")
        await page.evaluate("""() => {
            const btn = document.querySelector('button#ver_online2, button[name="boton"]');
            if (btn) { btn.disabled = false; btn.removeAttribute('disabled'); }
        }""")

    if DEBUG:
        await page.screenshot(path="debug_filters.png")

    submit_btn = await page.query_selector(
        'button#ver_online2, button[name="boton"][value="Ver Online"]'
    )
    if submit_btn:
        btn_info = await page.evaluate(
            "(el) => ({tag: el.tagName, id: el.id, disabled: el.disabled, value: el.value})",
            submit_btn
        )
        print(f"  Click en Ver Online: {btn_info}")
        await submit_btn.click()
    else:
        print("  ADVERTENCIA: No se encontró Ver Online")

    await page.wait_for_load_state("networkidle", timeout=25_000)
    await page.wait_for_timeout(8000)

    print(f"  URL actual: {page.url}")

    score_frame = None
    for frame in page.frames:
        try:
            count = await frame.evaluate(
                "() => (document.body.innerText.match(/[\\u00ba\\u00b0]C/g) || []).length"
            )
            size = await frame.evaluate(
                "() => document.documentElement.outerHTML.length"
            )
            print(f"  Frame [{frame.url[:80]}]: {count} °C, {size:,} bytes")
            if count > 2 and score_frame is None:
                score_frame = frame
        except Exception as ex:
            print(f"  Frame [{frame.url[:60]}]: no accesible ({ex})")

    if score_frame:
        print(f"  Score Card en frame: {score_frame.url[:80]}")
    else:
        degree_count = await page.evaluate(
            "() => (document.body.innerText.match(/[\\u00ba\\u00b0]C/g) || []).length"
        )
        print(f"  Usando frame principal ({degree_count} lecturas ºC)")

    if DEBUG:
        await page.screenshot(path="debug_scorecard.png", full_page=True)
        target = score_frame or page
        html = await target.content()
        with open("debug_scorecard.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  debug_scorecard.html guardado ({len(html):,} bytes)")

    return score_frame


MAX_RETRIES = 3
RETRY_DELAY_SECS = 15


async def _run_attempt(attempt: int) -> None:
    """Ejecuta un intento completo de chequeo usando su propio browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            now_chile = datetime.now(CHILE_TZ)
            ts = now_chile.strftime("%H:%M:%S")
            print(f"[{ts}] Intento {attempt}/{MAX_RETRIES} — Iniciando chequeo T-Sensor...")

            state = await read_state()
            alerted = _clean_stale_alerts(state.get("alerted", {}))

            score_frame = await navigate_to_scorecard(page)
            target = score_frame if score_frame else page

            print("  Buscando sensores en rojo...")
            red_items = await get_red_sensors_computed(target)
            print(f"  Sensores rojos encontrados (1er scan): {len(red_items)}")

            if not red_items:
                await (score_frame or page).wait_for_timeout(5000)
                red_items = await get_red_sensors_computed(target)
                print(f"  Sensores rojos encontrados (2do scan): {len(red_items)}")

            current_red = {_extract_name(item["text"]): item for item in red_items}

            recovered = [name for name in alerted if name not in current_red]
            for name in recovered:
                print(f"  ✅ Recuperado: {name}")
                del alerted[name]

            new_red = {name: item for name, item in current_red.items() if name not in alerted}

            if new_red:
                now_str = now_chile.strftime("%H:%M  %d/%m/%Y")
                n = len(new_red)
                label = "sensor fuera de rango" if n == 1 else "sensores fuera de rango"
                sep = "─" * 22
                msg = f"🚨 <b>ALERTA T-SENSOR</b> — {now_str}\n"
                msg += f"<b>{n} {label}</b>\n\n"
                for name, item in list(new_red.items())[:20]:
                    msg += f"{sep}\n{_format_sensor(item['text'])}\n"
                msg += sep
                if len(new_red) > 20:
                    msg += f"\n\n… y {len(new_red) - 20} más."
                print(f"  ALERTA: {n} sensores nuevos en rojo.")
                sent = await send_telegram(msg)

                if sent:
                    alert_time = now_chile.isoformat()
                    for name in new_red:
                        alerted[name] = alert_time
                else:
                    print(f"  ⚠️ Telegram falló — {n} sensor(es) NO marcados, se reintentará en próximo run.")
            else:
                if current_red:
                    print(f"  {len(current_red)} sensor(es) en rojo ya notificado(s) — sin nueva alerta.")
                else:
                    print("  Todo normal — sin alertas.")

            # El run fue exitoso: si el servidor venía caído, limpiar el registro.
            # NO se avisa al grupo (de la caída/recuperación se encarga el bot de health).
            prev_down = state.get("site_down", False)
            _, recovered, new_down = _outage_transition(prev_down, run_ok=True)
            if recovered:
                print("  ✅ Servidor recuperado — limpiando registro interno (sin avisar al grupo).")
            state["site_down"] = new_down
            if not new_down:
                state.pop("outage", None)

            state["alerted"] = alerted
            saved = await write_state(state)
            if not saved:
                print("  ⚠️ Estado NO guardado — próximo run podría re-alertar los mismos sensores.")
            await ping_healthcheck()

        except Exception:
            if DEBUG:
                try:
                    await page.screenshot(path="debug_error.png")
                except Exception:
                    pass
            raise
        finally:
            await browser.close()


async def monitor():
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await _run_attempt(attempt)
            return
        except Exception as e:
            last_error = e
            traceback.print_exc()
            if attempt < MAX_RETRIES:
                print(f"  Intento {attempt} falló. Reintentando en {RETRY_DELAY_SECS}s...")
                await asyncio.sleep(RETRY_DELAY_SECS)

    # Los 3 intentos fallaron — abortar.
    # NO se avisa al grupo de Telegram: de las caídas/recuperaciones del servidor
    # se encarga el bot de healthchecks.io. El /fail es justo lo que ese bot lee.
    await ping_healthcheck("/fail")

    # Guardamos el error INTERNAMENTE en state.json solo en la primera falla de la
    # racha (preserva el error original y desde cuándo), por si el servidor no vuelve.
    try:
        state = await read_state()
    except Exception as e:
        print(f"  No se pudo leer estado para registrar el error: {e}")
        state = {"alerted": {}}
    prev_down = state.get("site_down", False)
    record_error, _, _ = _outage_transition(prev_down, run_ok=False)

    if record_error:
        state["site_down"] = True
        state["outage"] = {
            "since": datetime.now(CHILE_TZ).isoformat(),
            "error": str(last_error)[:1500],
        }
        saved = await write_state(state)
        if saved:
            print("  Servidor no responde — error guardado internamente en state.json (sin avisar al grupo).")
        else:
            print("  ⚠️ Servidor no responde y NO se pudo guardar el registro del error.")
    else:
        print("  Servidor sigue sin responder — ya registrado, sin cambios.")

    raise last_error


if __name__ == "__main__":
    asyncio.run(monitor())
