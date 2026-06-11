FROM python:3.12-slim

ARG BRIDGE_VERSION="v4.8.1"
LABEL org.opencontainers.image.version="${BRIDGE_VERSION}"
LABEL org.opencontainers.image.title="EcoFlow DELTA Pro Bridge"
LABEL org.opencontainers.image.description="Local TCP→MQTT bridge for EcoFlow DELTA Pro (aa02 protocol)"
ENV BRIDGE_VERSION=${BRIDGE_VERSION}

WORKDIR /app

RUN pip install --no-cache-dir paho-mqtt==2.1.0

# Bridge code (translations are mounted at runtime so edits don't require a rebuild)
COPY ecoflow_codec.py ecoflow_receive.py ecoflow_send.py dp_bridge.py /app/

ENV PYTHONUNBUFFERED=1

CMD ["python3", "-u", "dp_bridge.py"]
