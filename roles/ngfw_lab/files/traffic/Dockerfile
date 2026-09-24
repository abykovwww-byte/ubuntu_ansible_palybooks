FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iperf3 iproute2 iputils-ping \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/traffic
COPY endpoint.py job.py policy_probe.py probe_diagnostics.py held_receiver.py ./
USER 65534:65534
ENTRYPOINT ["python3", "/opt/traffic/endpoint.py"]
CMD ["server"]
