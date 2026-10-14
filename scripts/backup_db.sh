#!/bin/sh
# Nightly compressed dump of the tethera database, kept 14 days, outside the repo.
set -e
DEST="$HOME/Backups/tethera"; mkdir -p "$DEST"
/usr/local/bin/pg_dump -Fc tethera > "$DEST/tethera-$(date +%Y%m%d).dump"
find "$DEST" -name 'tethera-*.dump' -mtime +14 -delete
echo "$(date '+%F %T') backup ok: $(ls -la "$DEST" | tail -1)"
