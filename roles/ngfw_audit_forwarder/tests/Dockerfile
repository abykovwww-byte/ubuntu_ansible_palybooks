FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends rsyslog python3 python3-jinja2 \
    && rm -rf /var/lib/apt/lists/*
COPY templates/rsyslog.conf.j2 /test-source.conf
COPY tests/test_runtime.py /test_runtime.py
ENTRYPOINT ["python3", "/test_runtime.py"]
