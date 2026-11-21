# Telegram Business Assistant Bot - Dockerfile for Railway
# Build: docker build --build-arg CLAUDE_VERSION=1.0.58 -t tg-business-assistant .
# The CLAUDE_VERSION should match the version verified locally with --check-claude

ARG CLAUDE_VERSION=1.0.58

FROM python:3.12-slim

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PATH="/root/.local/bin:${PATH}"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI with specific version
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
    && claude --version

# Set working directory
WORKDIR /app

# Install Python dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run as root (Railway Volume mounts as root)
CMD ["python", "bot.py", "--settings-from-env"]