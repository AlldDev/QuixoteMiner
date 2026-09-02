"""Cobertura de telemetry/ipc.py: servidor de socket Unix de verdade."""

import json
import socket
import threading
import time

from quixote.telemetry.ipc import serve_forever
from quixote.telemetry.state import SharedState


def test_cliente_recebe_json_valido_com_estado_atual(tmp_path):
    sock_path = tmp_path / "quixote.sock"
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.record_share_accepted()

    running = threading.Event()
    running.set()
    server_thread = threading.Thread(
        target=serve_forever,
        args=(state, sock_path),
        kwargs={"should_continue": running.is_set},
        daemon=True,
    )
    server_thread.start()

    try:
        deadline = time.monotonic() + 2
        while not sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(sock_path))
        f = client.makefile("r", encoding="utf-8", newline="\n")
        line = f.readline()
        payload = json.loads(line)

        assert payload["shares_accepted"] == 1
        assert "hashrate_instant" in payload
        assert "connection_state" in payload

        f.close()
        client.close()
    finally:
        running.clear()
        server_thread.join(timeout=2)


def test_permissao_do_socket_e_0600(tmp_path):
    sock_path = tmp_path / "quixote.sock"
    state = SharedState(persistence_path=tmp_path / "state.json")

    running = threading.Event()
    running.set()
    server_thread = threading.Thread(
        target=serve_forever,
        args=(state, sock_path),
        kwargs={"should_continue": running.is_set},
        daemon=True,
    )
    server_thread.start()

    try:
        deadline = time.monotonic() + 2
        while not sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        running.clear()
        server_thread.join(timeout=2)
