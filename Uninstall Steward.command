#!/bin/bash
# Double-click to stop Steward and remove its background service and app.
cd "$(dirname "$0")" && bash scripts/uninstall.sh; read -n1 -r -p "Press any key to close."
