#!/bin/sh
# A bind mount can exist while the encrypted source dataset is unavailable.
set -eu
mount=${1:-/srv/reeldub-private/spool}
case $(findmnt -rn -M "$mount" -t zfs -o SOURCE) in
    */reeldub-intake-private/spool) ;;
    *) echo "ReelDub intake requires its encrypted spool dataset." >&2; exit 1 ;;
esac
