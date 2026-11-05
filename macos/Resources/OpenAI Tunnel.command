#!/bin/zsh
set +x
set +v
launcher_dir="${0:A:h}"
"${launcher_dir}/../MacOS/HFPilotBackend" tunnel
launcher_status=$?
if (( launcher_status != 0 && launcher_status != 130 )); then
    print '\nThe tunnel did not start. Review the message above and try again.'
fi
print '\nYou can close this window. HFPilot is managed separately.'
read -r '?Press Return to close.'
exit "$launcher_status"
