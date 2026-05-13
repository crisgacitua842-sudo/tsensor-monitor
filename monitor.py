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
from playwright.async_api import async_playwright
from datetime import datetime

TSENSOR_URL   = os.environ.get("TSENSOR_URL", "https://app.tsensor.online/informes/menu/98")
TSENSOR_USER  = os.environ.get("TSENSOR_USER")
TSENSOR_PASS  = os.environ.get("TSENSOR_PASS")
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


async def get_red_sensors(page):
    """
    Busca tarjetas de sensores con fondo rojo (temperatura fuera de rango crítico).
    Solo considera elementos que contengan '°C' para evitar falsos positivos
    con otros elementos rojos de la UI (logos, iconos, etc).
    """
    sensors = await page.evaluate("""() => {
        function isRed(colorStr) {
            if (!colorStr) return false;
            const m = colorStr.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!m) return false;
            const [r, g, b] = [+m[1], +m[2], +m[3]];
            return r > 150 && g < 100 && b < 100;
        }

        const results = [];
        const seen = new Set();

        for (const el of document.querySelectorAll('*')) {
            const style = window.getComputedStyle(el);
            if (!isRed(style.backgroundColor)) continue;

            const text = (el.innerText || '').trim();

            // Solo tarjetas de sensores: deben contener °C y un nombre de sensor
            if (!text.includes('°C')) continue;
            if (text.length > 400 || text.length < 10) continue;
            if (seen.has(text)) continue;
            seen.add(text);

            results.push({ text: text.replace(/\\s+/g, ' ') });
        }
        return results;
    }""")
    return sensors


async def login(page):
    print("  Cargando página de login...")
    await page.goto(TSENSOR_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2000)

    # Llenar usuario
    for sel in ['input[type="text"]', 'input[name*="user" i]',
                'input[placeholder*="usuario" i]', 'input[id*="user" i]']:
        el = await page.query_selector(sel)
        if el:
            await el.fill(TSENSOR_USER)
            break

    # Llenar contraseña
    await page.fill('input[type="password"]', TSENSOR_PASS)

    # Click en botón ENTRAR
    for sel in ['button:has-text("ENTRAR")', 'button:has-text("Entrar")',
                'button[type="submit"]', 'input[type="submit"]']:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            break

    await page.wait_for_load_state("networkidle", timeout=20_000)
    await page.wait_for_timeout(4000)  # Espera contenido dinámico

    if DEBUG:
        await page.screenshot(path="debug_login.png")
        print("  Screenshot guardado: debug_login.png")


async def monitor():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        try:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] Iniciando chequeo T-Sensor...")

            await login(page)

            print("  Buscando sensores en rojo...")
            red_items = await get_red_sensors(page)

            if DEBUG:
                print("Elementos rojos:", json.dumps(red_items, indent=2, ensure_ascii=False))

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
                print(f"  Todo normal — sin alertas.")

        except Exception as e:
            print(f"Error: {e}")
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
