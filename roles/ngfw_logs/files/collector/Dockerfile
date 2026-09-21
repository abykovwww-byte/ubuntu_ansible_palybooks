FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      syslog-ng-core syslog-ng-mod-json logrotate python3-minimal \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin collector \
    && mkdir -p /data /var/lib/syslog-ng \
    && chown 10001:10001 /data /var/lib/syslog-ng
COPY collector.py /opt/collector/collector.py
USER 10001:10001
ENTRYPOINT ["python3", "/opt/collector/collector.py"]
CMD ["serve"]
