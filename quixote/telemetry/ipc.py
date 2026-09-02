"""Servidor de socket Unix que expõe o SharedState em JSON, uma linha por vez.

O painel (`quixote top`) é o cliente natural, mas qualquer processo pode
conectar e ler — é por isso que roda num socket separado do processo do
daemon: fechar o painel não para a mineração.
"""

import json
import logging
import os
import pathlib
import socket
import threading
import time
from collections.abc import Callable

from quixote.telemetry.state import SharedState

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "quixote.sock"
PUSH_INTERVAL_SECONDS = 0.25


def _handle_client(
    conn: socket.socket, state: SharedState, should_continue: Callable[[], bool]
) -> None:
    try:
        while should_continue():
            line = json.dumps(state.to_dict()) + "\n"
            conn.sendall(line.encode("utf-8"))
            time.sleep(PUSH_INTERVAL_SECONDS)
    except OSError:
        pass  # cliente desconectou, nada a fazer
    finally:
        conn.close()


def serve_forever(
    state: SharedState,
    sock_path: pathlib.Path = DEFAULT_SOCKET_PATH,
    should_continue: Callable[[], bool] = lambda: True,
) -> None:
    """Sobe o servidor e fica aceitando conexões até `should_continue()` virar `False`.

    Cada cliente conectado recebe uma linha JSON com o estado atual a cada
    `PUSH_INTERVAL_SECONDS`, numa thread própria por conexão.

    Args:
        state: o `SharedState` a expor.
        sock_path: caminho do socket Unix. Removido e recriado com
            permissão 0600 a cada subida do servidor.
        should_continue: checado entre `accept()`s e a cada push; permite
            desligar o servidor de fora (usado em teste e no shutdown do
            daemon).
    """
    if sock_path.exists():
        sock_path.unlink()

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(sock_path))
    sock_path.chmod(0o600)
    server_sock.listen(5)
    server_sock.settimeout(0.5)
    logger.info("servidor IPC escutando em %s", sock_path)

    try:
        while should_continue():
            try:
                conn, _ = server_sock.accept()
            except TimeoutError:
                continue
            thread = threading.Thread(
                target=_handle_client, args=(conn, state, should_continue), daemon=True
            )
            thread.start()
    finally:
        server_sock.close()
        if sock_path.exists():
            sock_path.unlink()
