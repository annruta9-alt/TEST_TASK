#!/bin/sh
set -e
# Load Apache's own variables but keep the container environment
# (DB_* must reach the mod_wsgi processes, so we do not use apache2ctl: it wipes the env).
. /etc/apache2/envvars
mkdir -p "$APACHE_RUN_DIR" "$APACHE_LOCK_DIR" "$APACHE_LOG_DIR"
rm -f "$APACHE_PID_FILE"          # stale pid file after a container restart
chown www-data:www-data /app/uploads
exec apache2 -D FOREGROUND
