FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# This runtime image has NO raw data and needs NO OCR engine: curation is local.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mri
USER 10001:10001
ENV MRI_BIND=0.0.0.0 MRI_PORT=8000 MRI_DATASET=/dataset MRI_TOKEN_FILE=/run/secrets/mri_token
EXPOSE 8000
CMD ["knee-mri-server"]
