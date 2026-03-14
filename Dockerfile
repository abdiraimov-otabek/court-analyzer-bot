# Use a slim Python image
FROM python:3.11-slim

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml .

# Install dependencies using uv
# --system flag tells uv to install into the image's python environment
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy the rest of the application
COPY . .

# Default command (can be overridden in docker-compose)
CMD ["python", "-m", "src.app.run_bot"]
