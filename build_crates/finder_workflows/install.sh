#!/bin/zsh
set -euo pipefail

workflow_dir="${0:A:h}"
finder_services_dir="${FINDER_SERVICES_DIR:-$HOME/Library/Services}"

/bin/mkdir -p "$finder_services_dir"
for workflow in "$workflow_dir"/*.workflow; do
  destination="$finder_services_dir/${workflow:t}"
  /usr/bin/ditto "$workflow" "$destination"
  print "Installed ${workflow:t}"
done

print "Enable the actions in System Settings > Privacy & Security > Extensions > Finder."
