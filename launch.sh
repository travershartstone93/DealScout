#!/usr/bin/env bash
# Desktop launcher: make sure the services are up, then open the dashboard in its own window.
systemctl --user start dealscout dealscout-term 2>/dev/null
for _ in $(seq 1 20); do curl -fs -o /dev/null http://127.0.0.1:5006/ && break; sleep 0.5; done
exec firefox --new-window "http://127.0.0.1:5006/"
