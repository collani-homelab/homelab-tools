FROM python:3.12-slim

WORKDIR /workspace

# Install system dependencies that might be needed to compile wheels, plus git
# (requirements.txt installs llm-eval-kit via a git+https URL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set execution environment variables
ENV PYTHONUNBUFFERED=1
