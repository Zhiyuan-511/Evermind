FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (docker-optimized: no GUI/browser deps)
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi>=0.110.0 \
    "uvicorn[standard]>=0.27.0" \
    websockets>=12.0 \
    openai>=1.60.0 \
    litellm>=1.55.0 \
    python-dotenv>=1.0.0 \
    Pillow>=10.2.0 \
    aiofiles>=23.2.0 \
    pydantic>=2.5.0 \
    cryptography>=44.0.0

# Copy backend code
COPY . .

# Create output directory
RUN mkdir -p /tmp/evermind_output

EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=5s \
    CMD curl -f http://localhost:8765/api/health || exit 1

# Bind to 0.0.0.0 so Docker can route traffic in
ENV HOST=0.0.0.0
ENV PORT=8765

CMD ["python", "server.py"]
