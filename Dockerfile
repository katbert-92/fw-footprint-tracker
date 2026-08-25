# Image for the ingest endpoint. Built from the checkout rather than installed
# from git at container start: the repository is already here, and a service
# that reaches out to GitHub every time it boots fails for anyone behind a
# restrictive network.

FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fwtrack ./fwtrack

RUN pip install --no-cache-dir --root-user-action=ignore .

EXPOSE 8099
CMD ["fwtrack-server"]
