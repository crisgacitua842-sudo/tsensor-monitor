# Imagen oficial de Playwright: ya trae Chromium y las ~60 librerías de sistema
# que necesita. Eso elimina de raíz el paso apt-get (`playwright install-deps`)
# que en los runners de GitHub tardaba 6-10 min y provocaba las caídas falsas
# de healthchecks.
#
# La etiqueta debe coincidir con la versión de playwright de requirements.txt
# (1.44.0). Verificado que existe: jammy = Ubuntu 22.04 = Python 3.10.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Las dependencias primero: así el caché de Docker no se invalida al tocar el
# código, que es lo que más cambia.
COPY requirements.txt .
# tzdata: la imagen no trae la base de zonas horarias y el código usa
# ZoneInfo("America/Santiago") para mostrar la hora de Chile. En los runners de
# GitHub venía con el sistema; acá hay que pedirla. Es específica del contenedor,
# por eso va aquí y no en requirements.txt (que comparte con GitHub Actions).
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY monitor.py effects_guard.py loop.py ./

# Sin buffer: Railway lee stdout en vivo y queremos ver los logs al instante.
ENV PYTHONUNBUFFERED=1

CMD ["python", "loop.py"]
