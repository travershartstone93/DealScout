#!/usr/bin/env bash
# Thin launcher: ./dealscout.sh scan | judge | report | login <site> | sources
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m dealscout.scan "$@"
