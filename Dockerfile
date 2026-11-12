FROM python:3.11-slim-bookworm
WORKDIR /app
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
