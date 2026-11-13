set -euo pipefail
umask 077

timestamp=${1:?Usage: bash restore.sh YYYYMMDDTHHMMSSZ}
if ! [[ "$timestamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    printf 'Invalid backup timestamp\n' >&2
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

prefix=${BACKUP_S3_PREFIX:-hmm}
case "$prefix" in /*|*..*|*:*|*/) printf 'Invalid BACKUP_S3_PREFIX\n' >&2; exit 1 ;; esac
snapshot="target:${BACKUP_S3_BUCKET}/${prefix}/${timestamp}"
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
rclone copyto "$snapshot/complete" "$temporary/complete"
if [ "$(cat "$temporary/complete")" != "$timestamp" ]; then
    printf 'Backup completion marker does not match timestamp\n' >&2
    exit 1
fi
rclone copyto "$snapshot/hmm.dump" "$temporary/hmm.dump"
rclone copyto "$snapshot/hmm_parser.dump" "$temporary/hmm_parser.dump"
rclone copyto "$snapshot/objects.list" "$temporary/objects.list"
pg_restore --list "$temporary/hmm.dump" > /dev/null
pg_restore --list "$temporary/hmm_parser.dump" > /dev/null
mkdir "$temporary/objects"
if [ -s "$temporary/objects.list" ]; then
    rclone copy "$snapshot/objects" "$temporary/objects"
fi
printf 'Restoring backup %s\n' "$timestamp"
pg_restore --dbname=postgres --create --clean --if-exists --exit-on-error --no-owner --no-acl "$temporary/hmm.dump"
pg_restore --dbname=postgres --create --clean --if-exists --exit-on-error --no-owner --no-acl "$temporary/hmm_parser.dump"
rclone sync "$temporary/objects" "source:${S3_BUCKET}" --delete-after
printf 'Restore complete: %s\n' "$timestamp"
