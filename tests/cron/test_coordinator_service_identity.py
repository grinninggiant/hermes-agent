"""Classify existing launchd service identity, never execute a lifecycle command."""
import subprocess
import pytest
from cron.lifecycle_guard import contains_gateway_lifecycle_command


@pytest.fixture
def service_processes(monkeypatch):
    def read_process_state(argv, **kwargs):
        if argv[:2] == ['/bin/launchctl', 'print']:
            pid = 501 if argv[-1].endswith('gateway-general') else 502
            return subprocess.CompletedProcess(argv, 0, stdout=f'\tpid = {pid}\n')
        assert argv == ['/bin/ps', '-axo', 'pid=,ppid=,command=']
        return subprocess.CompletedProcess(argv, 0, stdout=(
            '501 1 /opt/hermes/venv/bin/hermes gateway run\n'
            '502 1 /usr/bin/python3 /opt/companion/coordinator.py run\n'))
    monkeypatch.setattr(subprocess, 'run', read_process_state)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize('snapshot', [
    '502 1 /bin/sh /opt/service.sh\n503 502 /opt/hermes/bin/hermes gateway run\n',
    '502 1 ???\n',
    '502 1 /usr/bin/python3 <defunct>\n',
    'malformed\n',
    '503 1 /usr/bin/python3 /opt/unrelated.py\n',
])
def test_unknown_or_gateway_descendant_cannot_gain_external_service_exemption(monkeypatch, snapshot):
    def probe(argv, **kwargs):
        output = '\tpid = 502\n' if argv[:2] == ['/bin/launchctl', 'print'] else snapshot
        return subprocess.CompletedProcess(argv, 0, stdout=output)
    monkeypatch.setattr(subprocess, 'run', probe)
    assert contains_gateway_lifecycle_command('launchctl kickstart -k gui/501/ai.hermes.gateway-restart-coordinator')


@pytest.mark.platforms("macos")
def test_external_coordinator_label_is_not_automatically_gateway_identity(service_processes):
    command = 'launchctl kickstart -k gui/501/ai.hermes.gateway-restart-coordinator'
    assert not contains_gateway_lifecycle_command(command)


@pytest.mark.platforms("macos")
def test_actual_gateway_lifecycle_stays_guarded(service_processes):
    command = 'launchctl kickstart -k gui/501/ai.hermes.gateway-general'
    assert contains_gateway_lifecycle_command(command)
