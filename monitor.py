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


def _is_red_style(style: str) -> bool:
    if not style:
        return False
    # rgb(r, g, b)
    m = re.search(r'background(?:-color)?\s*:\s*rgb\((\d+),\s*(\d+),\s*(\d+)\)', style, re.I)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return r > 150 and g < 100 and b < 100
    # #rrggbb
    m = re.search(r'background(?:-color)?\s*:\s*#([0-9a-fA-F]{6})\b', style, re.I)
    if m:
        hx = m.group(1)
        r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
        return r > 150 and g < 100 and b < 100
    # #rgb
    m = re.search(r'background(?:-color)?\s*:\s*#([0-9a-fA-F]{3})\b', style, re.I)
    if m:
        hx = m.group(1)
        r, g, b = int(hx[0] * 2, 16), int(hx[1] * 2, 16), int(hx[2] * 2, 16)
        return r > 150 and g < 100 and b < 100
    return False


def parse_red_sensors(html: str) -> list:
    """Busca tarjetas de sensores con fondo rojo en el HTML del Score Card."""
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    seen = set()

    for el in soup.find_all(True):
        style = el.get('style', '')
        if not _is_red_style(style):
            continue
        text = el.get_text(separator=' ', strip=True)
        if '°C' not in text:
            continue
        if len(text) > 400 or len(text) < 10:
            continue
        if text in seen:
            continue
        seen.add(text)
        results.append({'text': re.sub(r'\s+', ' ', text)})

    return results


async def prepare_session(page) -> tuple:
    """
    Hace login con Playwright, navega hasta el formulario del Score Card,
    selecciona todos los filtros y devuelve (cookies, form_pairs, action_url).
    """
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

    # Después del login redirige al home — ir a TELEMETRÍA
    print("  Haciendo click en TELEMETRÍA...")
    await page.click('a:has-text("TELEMETRÍA"), a:has-text("Telemetría")')
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await page.wait_for_timeout(3000)

    if DEBUG:
        await page.screenshot(path="debug_login.png")

    # Mostrar Todos para cargar los elementos del formulario
    mostrar = await page.query_selector('input[value="Mostrar Todos"], button:has-text("Mostrar Todos")')
    if mostrar:
        await mostrar.click()
        print("  Click en Mostrar Todos, esperando carga...")
    await page.wait_for_timeout(6000)

    # Seleccionar Score Card vía JS
    await page.evaluate("""() => {
        const sel = document.querySelector('#InformeId');
        if (!sel) return;
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
    await page.wait_for_timeout(4000)

    # Seleccionar todas las opciones de los filtros
    await page.evaluate("""() => {
        for (const sel of document.querySelectorAll('select')) {
            if (sel.id === 'InformeId') continue;
            for (const opt of sel.options) opt.selected = true;
        }
        const inf = document.querySelector('#InformeId');
        if (!inf) return;
        for (const opt of inf.options) {
            if (opt.text.trim() === 'Score Card') { inf.value = opt.value; break; }
        }
    }""")

    if DEBUG:
        await page.screenshot(path="debug_after_filters.png")

    # Extraer datos del formulario como lista de pares [nombre, valor]
    form_info = await page.evaluate("""() => {
        const form = document.querySelector('form');
        if (!form) return null;
        const pairs = [];
        for (const el of form.elements) {
            if (!el.name) continue;
            if (el.tagName === 'SELECT') {
                for (const opt of el.options) {
                    if (opt.selected) pairs.push([el.name, opt.value]);
                }
            } else if (el.type === 'checkbox' || el.type === 'radio') {
                if (el.checked) pairs.push([el.name, el.value]);
            } else if (el.type !== 'submit' && el.type !== 'button') {
                pairs.push([el.name, el.value || '']);
            }
        }
        return { action: form.action || window.location.href, pairs };
    }""")

    if not form_info:
        raise RuntimeError("No se encontró el formulario en la página")

    # Agregar el valor del botón submit
    form_info['pairs'].append(['boton', 'Ver Online'])

    # Cookies de la sesión activa
    cookies = await page.context.cookies()

    if DEBUG:
        print(f"  Pares de formulario: {len(form_info['pairs'])}")
        print(f"  Cookies: {len(cookies)}")
        print(f"  URL de acción: {form_info['action']}")

    return cookies, form_info['pairs'], form_info['action']


async def fetch_scorecard(cookies: list, form_pairs: list, action_url: str) -> str:
    """Envía el POST del Score Card directamente con aiohttp usando las cookies de Playwright."""
    cookie_header = '; '.join(f"{c['name']}={c['value']}" for c in cookies)

    headers = {
        'Cookie': cookie_header,
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        ),
        'Referer': action_url,
        'Origin': 'https://app.tsensor.online',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'es-CL,es;q=0.9',
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            action_url,
            data=form_pairs,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            print(f"  POST Score Card: status={resp.status}, url={resp.url}")
            html = await resp.text()
            print(f"  Respuesta HTML: {len(html):,} bytes")
            if DEBUG:
                with open("debug_scorecard.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print("  HTML guardado: debug_scorecard.html")
            return html


async def monitor():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        try:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] Iniciando chequeo T-Sensor...")

            # 1. Login + preparar formulario con Playwright
            cookies, form_pairs, action_url = await prepare_session(page)

            # 2. POST directo con aiohttp (evita el problema de renderizado headless)
            print("  Obteniendo Score Card vía HTTP directo...")
            html = await fetch_scorecard(cookies, form_pairs, action_url)

            # 3. Parsear HTML en busca de sensores rojos
            print("  Buscando sensores en rojo...")
            red_items = parse_red_sensors(html)

            if DEBUG:
                print(f"  Sensores rojos encontrados: {len(red_items)}")
                for item in red_items[:5]:
                    print(f"    {item['text'][:120]}")

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
