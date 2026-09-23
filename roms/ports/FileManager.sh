#!/bin/bash
# File manager, with the analog stick as a mouse for the duration of the session
# only (the mouse daemon is killed on exit).
MOUSE=/userdata/system/piboy-mouse.py
MOUSE_PID=""
cleanup() { [ -n "$MOUSE_PID" ] && kill "$MOUSE_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM
python3 "$MOUSE" --filemanager >/userdata/system/piboy-mouse.log 2>&1 &
MOUSE_PID=$!
sleep 1
filemanagerlauncher
