FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY . .

# Copy frontend HTML to be served
COPY ../evermind_godmode_final.html /app/../evermind_godmode_final.html 2>/dev/null || true

# Create output directory
RUN mkdir -p /tmp/evermind_output

EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=5s \
    CMD curl -f http://localhost:8765/api/health || exit 1

CMD ["python", "server.py"]
