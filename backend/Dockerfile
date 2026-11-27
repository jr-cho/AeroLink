# Backend + simulator image (PLAN E6.7). Build context is the repo root (needs airspace/, contracts/):
#   docker compose build backend
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/backend/.venv HF_HOME=/models/hf-cache
WORKDIR /app/backend

# libgomp: LightGBM's OpenMP runtime (the gbm forecaster).
RUN apt-get update -q && apt-get install -y -q --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app aerolink \
    && mkdir -p /models/hf-cache && chown aerolink /models/hf-cache

# Dependencies first (cached layer; only rebuilt when the lockfile changes).
# The lockfile pins the CUDA build of torch (~3 GB with the nvidia-* wheels). This image has no GPU, so
# everything else is installed exactly as locked and torch comes from PyTorch's CPU index (~200 MB).
# Keep the torch version below equal to the one in uv.lock.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv "$VIRTUAL_ENV" \
    && uv export --locked --no-dev --no-emit-project --no-hashes -o /tmp/req.txt \
    && grep -Ev '^(torch|triton|nvidia-|cuda-)' /tmp/req.txt > /tmp/req-nocuda.txt \
    && uv pip install --no-deps -r /tmp/req-nocuda.txt \
    && uv pip install --no-deps "torch==2.15.0" --index-url https://download.pytorch.org/whl/cpu

COPY airspace/ /app/airspace/
COPY contracts/ /app/contracts/
# backend/ brings models/, scenarios/, eval-results/ (API reads backend/eval-results) and data/ (minus the
# adsb.lol parquet segments, see .dockerignore).
COPY backend/ ./
# Editable install keeps the package under /app/backend/src, so code that finds the repo root from
# __file__ (service/settings.py, sim/live.py: parents[4] = /app) sees airspace/, scenarios/, models/.
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --no-deps -e .

ENV PATH="/app/backend/.venv/bin:$PATH" NETWORK_FILE=/app/airspace/central-fl-v1.geojson
USER aerolink
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"
CMD ["sh", "-c", "alembic upgrade head && exec python -m aerolink.service"]
