FROM akamai-base:latest

WORKDIR /app/

RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libcairo-gobject2 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libfontconfig1 \
    fonts-liberation \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libxshmfence1 \
    libxss1 \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libasound2 \
    libx11-xcb1 \
    libxtst6 \
    libpci3 \
    libgl1 \
    libglx-mesa0 \
    libegl1 \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-common.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -U
RUN python -c "from cloakbrowser import ensure_binary; ensure_binary()"

COPY main.py solver.py utils.py /app/

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "main.py"]

