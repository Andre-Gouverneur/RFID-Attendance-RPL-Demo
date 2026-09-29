# Use Python 3.11 slim image
FROM python:3.11-slim

# Set non-privileged user and group
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Install system dependencies needed for Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Prevent .pyc files & enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set working directory to the project's root
WORKDIR /app

# Upgrade pip, setuptools, wheel
RUN pip install --upgrade pip setuptools wheel

# Copy requirements and install dependencies
# This is a key step to leverage Docker's build cache
COPY requirements_windows.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Change ownership of the app directory to the non-privileged user
RUN chown -R appuser:appuser /app

# Switch to the non-privileged user
USER appuser

# No CMD or ENTRYPOINT here. The command will be defined in docker-compose.yml.