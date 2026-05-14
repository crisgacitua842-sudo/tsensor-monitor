#!/usr/bin/env python3
"""
T-Sensor temperature monitor
Sends Telegram alerts when any sensor turns red (out of range).
"""

import asyncio
import os
import re
import json
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from datetime import datetime

TSENSOR_URL      = os.environ.get("TSENSOR_URL", "https://app.tsensor.online/informes/menu/98")
TSENSOR_USER     = os.environ.get("TSENSOR_USER")
TSENSOR_PASS     = os.environ.get("TSENSOR_PASS")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"


async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"Error Telegram {resp.status}: {body}")
            else:
                print("Alerta enviada por Telegram.")


async def get_red_sensors_computed(page) -> list:
    """
    Detecta sensores rojos. Los colores se aplican como background:linear-gradient(hex1,hex2)
    en el atributo style inline — getComputedStyle.backgroundColor devuelve transparente
    para gradientes, así que parseamos el atributo style directamente.
    """
    return await page.evaluate("""() => {
        function isRedHex(hex) {
            const r = parseInt(hex.slice(1, 3), 16);
            const g = parseInt(hex.slice(3, 5), 16);
            const b = parseInt(hex.slice(5, 7), 16);
            return r > 150 && g < 100 && b < 100;
        }
        function hasRedBackground(el) {
            const style = el.getAttribute('style') || '';
            if (!style.includes('background')) return false;
            // Extraer colores hex del gradiente inline
            const hexColors = style.match(/#[0-9a-fA-F]{6}/g) || [];
            if (hexColors.some(isRedHex)) return true;
            // Fallback: background-color sólido vía getComputedStyle
            const bg = window.getComputedStyle(el).backgroundColor;
            const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (m) {
                const [r, g, b] = [+m[1], +m[2], +m[3]];
                return r > 150 && g < 100 && b < 100;
            }
            return false;
        }
        function hasDegreeC(text) {
            // ISO-8859-1: º = U+00BA (ordinal), ° = U+00B0 (grado)
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


def _is_red_inline(style: str) -> bool:
    """Verifica si un style inline tiene fondo rojo."""
    if not style:
        return False
    m = re.search(r'background(?:-color)?\s*:\s*rgb\((\d+),\s*(\d+),\s*(\d+)\)', style, re.I)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return r > 150 and g < 100 and b < 100
    m = re.search(r'background(?:-color)?\s*:\s*#([0-9a-fA-F]{6})\b', style, re.I)
    if m:
        hx = m.group(1)
        r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
        return r > 150 and g < 100 and b < 100
    m = re.search(r'background(?:-color)?\s*:\s*#([0-9a-fA-F]{3})\b', style, re.I)
    if m:
        hx = m.group(1)
        r, g, b = int(hx[0]*2, 16), int(hx[1]*2, 16), int(hx[2]*2, 16)
        return r > 150 and g < 100 and b < 100
    return False


def get_red_sensors_html(html: str) -> list:
    """Detecta sensores rojos buscando estilos inline en el HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    seen = set()
    for el in soup.find_all(True):
        if not _is_red_inline(el.get('style', '')):
            continue
        text = el.get_text(separator=' ', strip=True)
        if '°C' not in text and 'ºC' not in text:
            continue
        if len(text) > 400 or len(text) < 10:
            continue
        if text in seen:
            continue
        seen.add(text)
        results.append({'text': re.sub(r'\s+', ' ', text)})
    return results


async def navigate_to_scorecard(page):
    """Login y navega hasta cargar el Score Card."""
    print("  Cargando página de login...")
    await page.goto(TSENSOR_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2000)

    # Usuario
    for sel in ['input[type="text"]', 'input[name*="user" i]',
                'input[placeholder*="usuario" i]', 'input[id*="user" i]']:
        el = await page.query_selector(sel)
        if el:
            await el.fill(TSENSOR_USER)
            break

    # Contraseña y login
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

    # "Mostrar Todos" es un <a> con javascript:update_select(...), NO un button/input
    mostrar = await page.query_selector(
        'a:has-text("Mostrar Todos"), input[value="Mostrar Todos"], button:has-text("Mostrar Todos")'
    )
    if mostrar:
        await mostrar.click()
        print("  Click en Mostrar Todos (AJAX cascadeante), esperando carga...")
    else:
        print("  ADVERTENCIA: No se encontró Mostrar Todos")
    # Esperar a que el AJAX cascadeante llene todos los dropdowns
    await page.wait_for_timeout(8000)

    if DEBUG:
        await page.screenshot(path="debug_after_mostrar.png")

    # Seleccionar Score Card en #InformeId
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

    # Esperar a que el JS habilite el botón Ver Online
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

    # Click en Ver Online (ya habilitado — sin force)
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

    # Esperar carga de la respuesta
    await page.wait_for_load_state("networkidle", timeout=25_000)
    await page.wait_for_timeout(8000)

    print(f"  URL actual: {page.url}")

    # Buscar el Score Card en TODOS los frames (puede estar en un iframe)
    # Nota: la página usa ISO-8859-1, el símbolo grado es º (U+00BA), no ° (U+00B0)
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
        score_frame = None

    if DEBUG:
        await page.screenshot(path="debug_scorecard.png", full_page=True)
        target = score_frame or page
        html = await target.content()
        with open("debug_scorecard.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  debug_scorecard.html guardado ({len(html):,} bytes)")

    return score_frame  # None = usar page; Frame = usar ese frame


async def monitor():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        try:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] Iniciando chequeo T-Sensor...")

            score_frame = await navigate_to_scorecard(page)
            target = score_frame if score_frame else page

            print("  Buscando sensores en rojo...")
            red_items = await get_red_sensors_computed(target)
            print(f"  Sensores rojos encontrados: {len(red_items)}")

            if red_items:
                now = datetime.now().strftime("%H:%M  %d/%m/%Y")
                msg = f"🚨 <b>Alerta T-Sensor</b> — {now}\n\n"
                msg += f"<b>{len(red_items)} sensor(es) fuera de rango:</b>\n\n"
                for item in red_items[:20]:
                    msg += f"• {item['text']}\n"
                if len(red_items) > 20:
                    msg += f"\n… y {len(red_items) - 20} más."
                print(f"  ALERTA: {len(red_items)} sensores fuera de rango.")
                await send_telegram(msg)
            else:
                print("  Todo normal — sin alertas.")

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
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
