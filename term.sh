#!/usr/bin/env bash
# Runs the in-app Claude Code terminal (persistent tmux session so page reloads keep the conversation).
cd "$HOME/dealscout" || exit 1
exec tmux -f "$HOME/dealscout/tmux.conf" new-session -A -s dealscout -c "$HOME/dealscout" claude
