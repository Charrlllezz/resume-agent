# Playwright's own image, because the hard part of containerising this is
# Chromium's system dependencies and they are already solved here. The version
# is pinned: an unpinned base means the browser rendering your PDF can change
# under you between two deploys of identical code.
# This tag must match the playwright pin in requirements.txt exactly. The
# image ships the browsers that version of the package expects; if the pip
# install outruns the image, Playwright looks for a browser build that was
# never baked in and the first PDF fails.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

# The resume stylesheet asks for IBM Plex and Space Grotesk, and pulls them
# from Google Fonts at print time. If that fetch fails the page still renders
# -- with fallback fonts, different metrics, and a different page height,
# silently. Installing them locally means the same PDF comes out whether or
# not the container can reach Google.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-ibm-plex \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /usr/share/fonts/truetype/space-grotesk \
 && for w in Regular Medium Bold; do \
      curl -fsSL -o "/usr/share/fonts/truetype/space-grotesk/SpaceGrotesk-$w.ttf" \
      "https://github.com/floriankarsten/space-grotesk/raw/master/fonts/ttf/SpaceGrotesk-$w.ttf"; \
    done \
 && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Nothing here writes to the image, and a resume should not be able to.
RUN useradd -m runner && chown -R runner:runner /app
USER runner

ENV RESUME_AGENT_HOSTED=1 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# ONE worker, many threads. Sessions -- including the API key someone typed in
# -- live in this process's memory. A second worker is a second, separate set
# of them, so half a user's requests would land somewhere that has never heard
# of their session. Scaling means threads here and more machines behind Fly's
# proxy, not more workers.
#
# --timeout 300 because a tailoring run legitimately takes 40-90s and gunicorn
# kills a worker that looks stuck; the default 30s would kill every run.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--bind", "0.0.0.0:8080", "--access-logfile", "-", "app:app"]
