FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (e.g., ffmpeg for media streaming)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY ./app /app

# Expose port
EXPOSE 8000

# Command to run the application (placeholder for FastAPI/Flask)
CMD ["python", "-m", "http.server", "8000"]
