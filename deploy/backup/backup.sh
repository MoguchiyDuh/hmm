set -euo pipefail
umask 077

if [ "${1:-backup}" = schedule ]; then
    printf '%s bash /usr/local/bin/backup.sh backup\n' "${BACKUP_SCHEDULE:-0 3 * * *}" > /tmp/backup.cron
    exec supercronic /tmp/backup.cron
fi
if [ "${1:-backup}" != backup ]; then
    printf 'Usage: bash backup.sh [backup|schedule]\n' >&2
    exit 1
fi

: "${PGPASSWORD:?}" "${S3_ENDPOINT:?}" "${S3_ACCESS_KEY:?}" "${S3_SECRET_KEY:?}" "${S3_BUCKET:?}"
: "${BACKUP_S3_ENDPOINT:?}" "${BACKUP_S3_BUCKET:?}" "${BACKUP_S3_ACCESS_KEY:?}" "${BACKUP_S3_SECRET_KEY:?}"
if [ "${BACKUP_S3_ENDPOINT%/}" = "${S3_ENDPOINT%/}" ] && [ "$BACKUP_S3_BUCKET" = "$S3_BUCKET" ]; then
    printf 'Backup target must not be the source bucket\n' >&2
    exit 1
fi
export RCLONE_CONFIG_SOURCE_TYPE=s3 RCLONE_CONFIG_SOURCE_PROVIDER=Other
export RCLONE_CONFIG_SOURCE_ENDPOINT="$S3_ENDPOINT"
export RCLONE_CONFIG_SOURCE_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export RCLONE_CONFIG_SOURCE_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
export RCLONE_CONFIG_TARGET_TYPE=s3 RCLONE_CONFIG_TARGET_PROVIDER=Other
export RCLONE_CONFIG_TARGET_ENDPOINT="$BACKUP_S3_ENDPOINT"
export RCLONE_CONFIG_TARGET_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY"
export RCLONE_CONFIG_TARGET_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_KEY"
export RCLONE_CONFIG_TARGET_REGION="${BACKUP_S3_REGION:-us-east-1}"
export RCLONE_CONFIG_SOURCE_FORCE_PATH_STYLE=true RCLONE_CONFIG_TARGET_FORCE_PATH_STYLE=true

retention=${BACKUP_RETENTION_DAYS:-30}
case "$retention" in ''|*[!0-9]*|0) printf 'Invalid BACKUP_RETENTION_DAYS\n' >&2; exit 1 ;; esac
prefix=${BACKUP_S3_PREFIX:-hmm}
case "$prefix" in /*|*..*|*:*|*/) printf 'Invalid BACKUP_S3_PREFIX\n' >&2; exit 1 ;; esac
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="target:${BACKUP_S3_BUCKET}/${prefix}"
snapshot="${target}/${timestamp}"
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT

printf 'Starting backup %s\n' "$timestamp"
pg_dump --dbname=hmm --format=custom --file="$temporary/hmm.dump"
pg_dump --dbname=hmm_parser --format=custom --file="$temporary/hmm_parser.dump"
rclone lsf "source:${S3_BUCKET}" --recursive --files-only > "$temporary/objects.list"
rclone copy "source:${S3_BUCKET}" "$snapshot/objects"
rclone copy "$temporary" "$snapshot"
printf '%s\n' "$timestamp" > "$temporary/complete"
rclone copyto "$temporary/complete" "$snapshot/complete"

cutoff=$(date -u -d "$retention days ago" +%Y%m%dT%H%M%SZ)
rclone lsf "$target" --dirs-only > "$temporary/snapshots"
while IFS= read -r directory; do
    candidate=${directory%/}
    if [[ "$candidate" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] && [[ "$candidate" < "$cutoff" ]]; then
        rclone purge "$target/$candidate"
    fi
done < "$temporary/snapshots"
printf 'Backup complete: %s\n' "$timestamp"
