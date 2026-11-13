FROM postgres:18

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl rclone \
    && rm -rf /var/lib/apt/lists/*
RUN case "${TARGETARCH}" in \
        amd64) checksum=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
        arm64) checksum=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
        *) exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-${TARGETARCH}" -o /usr/local/bin/supercronic \
    && echo "${checksum}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod 755 /usr/local/bin/supercronic
COPY deploy/backup/backup.sh deploy/backup/restore.sh /usr/local/bin/
USER postgres
ENTRYPOINT ["bash", "/usr/local/bin/backup.sh"]
CMD ["schedule"]
