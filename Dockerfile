# One container: builds the Next.js site, then FastAPI serves it plus /api on $PORT.
# Works on Hugging Face Spaces (Docker SDK, port 7860) and Render (sets $PORT).
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/app backend/app
COPY backend/data/index backend/data/index
COPY --from=web /web/out frontend/out
RUN chown -R user:user /app
USER user
WORKDIR /app/backend
ENV PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
