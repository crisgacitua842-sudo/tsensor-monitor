#!/usr/bin/env python3
"""
T-Sensor temperature monitor
Sends Telegram alerts when any sensor turns red (out of range).
Only alerts once per incident — re-alerts if sensor recovers then goes red again.
"""

import asyncio
import base64
import os
import re
import json
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

CHILE_TZ = ZoneInfo("America/Santiago")

_SENSOR_RE = re.compile(
    r'(.+?)\s+([-\d.]+)\s*[º°]C\s+'
    r'Max:\s*([-\d.]+)[º°]C\s+'
    r'Min:\s*([-\d.]+)[º°]C\s+'
    r'(\d{1,2}\s+\w+\s+\d{4})\s+'
    r'(\d{2}:\d{2}:\d{2})\s+'
    r'T\.\s*Fuera\s+Rango:\s+(\d{2}:\d{2}:\d{2})',
    re.UNICODE,
)


def _fmt_duration(hms: str) -> str:
    parts = hms.split(':')
    h, m = int(parts[0]), int(parts[1])
    if h > 0:
        return f"{h}h {m:02d}min"
    return f"{m}min"


def _extract_name(raw: str) -> str:
    m = _SENSOR_RE.match(raw.strip())
    if m:
        return m.group(1).strip()
    return raw.strip().splitlines()[0].strip()[:100]


def _format_sensor(raw: str) -> str:
    m = _SENSOR_RE.match(raw.strip())
    if not m:
        return f"• {raw}"
    name, temp, max_t, min_t, fecha, hora, fuera = m.groups()
    duracion = _fmt_duration(fuera)
    hora_corta = hora[:5]  # HH:MM
    return (
        f"📍 <b>{name.strip()}</b>\n"
        f"   🌡 Temp: <b>{temp}°C</b>   Rango: {min_t}°C → {max_t}°C\n"
        f"   ⏱ Fuera de rango: <b>{duracion}</b>\n"
        f"   🕐 Último dato: {hora_corta}  {fecha}"
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


async def write_state(state: dict):
    """Guarda el estado actualizado en el repositorio GitHub."""
    if not GITHUB_TOKEN:
        return
    sha = state.pop("_sha", None)
    content = base64.b64encode(json.dumps(state, indent=2, ensure_ascii=False).encode()).decode()
    payload = {"message": "chore: update sensor state [skip ci]", "content": content}
    if sha:
        payload["sha"] = sha
    async with aiohttp.ClientSession() as session:
        async with session.put(GH_API, headers=GH_HEADERS, json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                print(f"  Error guardando estado: {resp.status} {body[:200]}")
            else:
                print("  Estado guardado en GitHub.")


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


async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"Error Telegram {resp.status}: {body}")
            else:
                print("  Alerta enviada por Telegram.")


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


async def navigate_to_scorecard(page):
    """Login y navega hasta cargar el Score Card."""
    print("  Cargando página de login...")
    await page.goto(TSENSOR_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2000)

    for sel in ['input[type="text"]', 'input[name*="user" i]',
                'input[placeholder*="usuario" i]', 'input[id*="user" i]']:
        el = await page.query_selector(sel)
        if el:
            await el.fill(TSENSOR_USER)
            break

    await page.fill('input[type="password"]', TSENSOR_PASS)
    for sel in ['button:has-text("ENTRAR")', 'button:has-text("Entrar")',
                'button[type="submit"]', 'input[type="submit"]']:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            break

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


async def monitor():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        try:
            now_chile = datetime.now(CHILE_TZ)
            ts = now_chile.strftime("%H:%M:%S")
            print(f"[{ts}] Iniciando chequeo T-Sensor...")

            # Leer estado anterior
            state = await read_state()
            alerted = state.get("alerted", {})

            score_frame = await navigate_to_scorecard(page)
            target = score_frame if score_frame else page

            print("  Buscando sensores en rojo...")
            red_items = await get_red_sensors_computed(target)
            print(f"  Sensores rojos encontrados: {len(red_items)}")

            # Nombres de sensores actualmente en rojo
            current_red = {_extract_name(item["text"]): item for item in red_items}

            # Sensores recuperados: estaban en alerted pero ya no están en rojo
            recovered = [name for name in alerted if name not in current_red]
            for name in recovered:
                print(f"  ✅ Recuperado: {name}")
                del alerted[name]

            # Nuevos sensores en rojo: están en rojo pero no habían sido alertados
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
                await send_telegram(msg)

                # Registrar como alertados
                alert_time = now_chile.isoformat()
                for name in new_red:
                    alerted[name] = alert_time
            else:
                if current_red:
                    print(f"  {len(current_red)} sensor(es) en rojo ya notificado(s) — sin nueva alerta.")
                else:
                    print("  Todo normal — sin alertas.")

            # Guardar estado actualizado
            state["alerted"] = alerted
            await write_state(state)

            # Confirmar a healthchecks.io que el run fue exitoso
            await ping_healthcheck()

        except Exception as e:
            import traceback
            traceback.print_exc()
            await ping_healthcheck("/fail")
            try:
                now_str = datetime.now(CHILE_TZ).strftime("%H:%M  %d/%m/%Y")
                await send_telegram(
                    f"⚠️ <b>ERROR en monitor T-Sensor</b> — {now_str}\n"
                    f"El sistema de alertas falló con el siguiente error:\n"
                    f"<code>{str(e)[:3500]}</code>\n\n"
                    f"Revisa GitHub Actions para más detalles."
                )
            except Exception:
                pass
            if DEBUG:
                try:
                    await page.screenshot(path="debug_error.png")
                except Exception:
                    pass
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(monitor())
