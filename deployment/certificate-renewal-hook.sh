#!/bin/sh
set -eu
case "${RENEWED_LINEAGE:-}" in
    /etc/letsencrypt/live/scanner.spacedo.org)
        exec /usr/bin/python3 /opt/github-key-scanner/reload-ingress.py
        ;;
esac
