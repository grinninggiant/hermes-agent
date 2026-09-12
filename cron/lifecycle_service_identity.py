"""Read-only process identity for literal maintenance of existing launchd jobs."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys


def verified_external_launchctl_target(text: str) -> bool:
    """True only when a literal existing-job kickstart targets no gateway process.

    Unknown syntax, unavailable process evidence and creation/removal operations
    retain the normal lifecycle guard. No service-name exception registry exists.
    """
    if sys.platform != 'darwin' or any(char in text for char in '\n\r;$`|&<>\\'):
        return False
    try:
        argv = shlex.split(text)
    except ValueError:
        return False
    if len(argv) != 4 or argv[0] not in {'launchctl', '/bin/launchctl'} or argv[1:3] != ['kickstart', '-k']:
        return False
    target = argv[3]
    if not re.fullmatch(r'(?:gui|user)/[0-9]+/[A-Za-z0-9_.-]+', target):
        return False

    from gateway.status import looks_like_gateway_command_line

    def service_pid() -> int:
        result = subprocess.run(['/bin/launchctl', 'print', target],
                                check=True, capture_output=True, text=True, timeout=5)
        match = re.search(r'^\s*pid\s*=\s*(\d+)\s*$', result.stdout, re.MULTILINE)
        if match is None:
            raise ValueError('service_pid_unavailable')
        return int(match.group(1))

    try:
        pid = service_pid()
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,command='],
                                check=True, capture_output=True, text=True, timeout=5)
        processes = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                raise ValueError('incomplete_process_snapshot')
            processes[int(parts[0])] = (int(parts[1]), parts[2])
        if pid not in processes or pid <= 1:
            return False
        descendants = {pid}
        while True:
            children = {child for child, (parent, _) in processes.items() if parent in descendants}
            expanded = descendants | children
            if expanded == descendants:
                break
            descendants = expanded
        for child in descendants:
            command = processes[child][1]
            # Missing/zombie process identity cannot establish a safe target.
            if command.startswith(('?', '(')) or '<defunct>' in command:
                return False
            if looks_like_gateway_command_line(command):
                return False
        return service_pid() == pid
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
