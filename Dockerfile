FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements-lock.txt ./
RUN python -m pip install -r requirements-lock.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY streamlit_app.py ./
COPY .streamlit/config.toml ./.streamlit/config.toml
RUN python -m pip install --no-deps . \
    && python -m support_ai.demo_pdfs --output /app/demo/demo_policies.pdf \
    && useradd --create-home --uid 10001 support \
    && mkdir -p /app/data \
    && chown -R support:support /app
USER support

FROM base AS test
COPY --chown=support:support tests ./tests
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
EXPOSE 8000 8501
CMD ["python", "-m", "support_ai.api"]
