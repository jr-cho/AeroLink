"""Run the backend: ``python -m aerolink.service`` (after ``alembic upgrade head``)."""

import logging
import os

import uvicorn

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
uvicorn.run(
    "aerolink.service.app:create_app",
    factory=True,
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    log_level="info",
)
