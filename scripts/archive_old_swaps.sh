#!/bin/sh
# Nightly: move raw swaps older than 14 days from the hot table (Mini SSD) to swaps_archive (external drive).
set -e
if [ ! -d /Volumes/TetheraData/pg/tethera_archive ]; then
  echo "$(date '+%F %T') archive SKIPPED: /Volumes/TetheraData not mounted" >&2; exit 1
fi
moved=$(/usr/local/bin/psql -d tethera -Atc "SELECT archive_old_swaps(14)")
echo "$(date '+%F %T') archived $moved swaps older than 14 days"
