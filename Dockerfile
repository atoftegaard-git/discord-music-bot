# --- Builder Stage ---
# This stage installs dependencies and builds a wheel for the application
FROM python:3.10-slim-bookworm AS builder

# Install system dependencies required for building some python packages and for the bot itself
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg

# Create a virtual environment
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt


# --- Final Stage ---
# This stage creates the final, lightweight image
FROM python:3.10-slim-bookworm AS final

# Install ffmpeg which is a runtime dependency
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Create a non-root user to run the application
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid 1001 --create-home appuser

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the application code
WORKDIR /home/appuser/app
COPY --chown=appuser:appuser bot.py .

# Explicitly create the default cache file and give ownership to the appuser
RUN touch .cache && chown appuser:appuser .cache

# Set the user and activate the virtual environment
USER appuser
ENV PATH="/opt/venv/bin:$PATH"

# Run the bot
CMD ["python", "bot.py"]
