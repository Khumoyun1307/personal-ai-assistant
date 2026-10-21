# Backup sidecar: pg_dump (matching the server major), age, rclone.
# ARCHITECTURE §14.2 / §15.4. Runs backup.sh in a loop (one dump per day).
FROM postgres:18
RUN apt-get update \
    && apt-get install --no-install-recommends -y age rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY backup.sh restore.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh
USER postgres
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
