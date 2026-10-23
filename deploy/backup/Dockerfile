# Backup sidecar: pg_dump (matching the server major), age, rclone.
# ARCHITECTURE §14.2 / §15.4. Runs backup.sh in a loop (one dump per day).
# Build context: deploy/backup/ (compose.prod.yml).
FROM postgres:18
RUN apt-get update \
    && apt-get install --no-install-recommends -y age rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY backup.sh restore.sh /usr/local/bin/
# /backups is a named volume. Docker initialises an empty named volume from the
# image's directory (contents and ownership), so creating it here as postgres
# lets the unprivileged sidecar write to it; without this the volume root is
# owned by root and every dump fails.
RUN chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
    && mkdir -p /backups/daily /backups/weekly \
    && chown -R postgres:postgres /backups \
    && chmod 0755 /backups
USER postgres
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
