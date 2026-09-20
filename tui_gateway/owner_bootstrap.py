"""Private native launch entrypoint; no public CLI owner flag or credential."""
import runpy
import socket
import sys
import threading

from tui_gateway.process_owner import protect_owner_channel, serve_owner_channel


def start_owner_channel(argv):
    # Only the explicit local General launch shape is supported, never ambient
    # HERMES_DESKTOP, inferred active profiles, remote connections or shell shims.
    if argv[:3] != ['--profile', 'general', 'serve']:
        raise ValueError('unsupported owner launch scope')
    channel = socket.socket(fileno=3)
    protect_owner_channel(channel)
    thread = threading.Thread(target=serve_owner_channel, args=(channel,),
                              name='native-process-owner', daemon=True)
    thread.start()
    return thread


if __name__ == '__main__':
    start_owner_channel(sys.argv[1:])
    runpy.run_module('hermes_cli.main', run_name='__main__')
