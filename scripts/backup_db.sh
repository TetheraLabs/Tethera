#!/bin/sh
# Nightly compressed dump of the tethera database to the external drive, kept 14 days.
# swaps_archive is excluded: it is reproducible from the chain and already lives on the same drive.
set -e
DEST="/Volumes/TetheraData/backups/tethera"
if [ ! -d /Volumes/TetheraData/backups ]; then
  echo "$(date '+%F %T') backup SKIPPED: /Volumes/TetheraData not mounted" >&2; exit 1
fi
mkdir -p "$DEST"
/usr/local/bin/pg_dump -Fc --exclude-table-data=swaps_archive tethera > "$DEST/tethera-$(date +%Y%m%d).dump"
find "$DEST" -name 'tethera-*.dump' -mtime +14 -delete
echo "$(date '+%F %T') backup ok: $(ls -la "$DEST" | tail -1)"
