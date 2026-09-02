"""Cliente Stratum v1, JSON-RPC sobre TCP puro delimitado por `\\n`.

Sequência de conexão: subscribe, authorize, suggest_difficulty, depois só
ficar recebendo `mining.notify` e `mining.set_difficulty`. Sem
`version-rolling` (recurso de ASIC, só adiciona superfície de erro) e sem
rolagem de `ntime` (usa exatamente o valor que o pool mandou).
"""

import json
import logging
import socket
import time
from collections.abc import Callable
from typing import Any, TextIO

from quixote.core.job import Job, Share

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_START_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 60.0
NOTIFY_TIMEOUT_SECONDS = 10 * 60


class StratumClient:
    """Cliente síncrono e bloqueante do protocolo Stratum v1.

    Concorrência (rodar isso numa thread própria enquanto o hasher roda em
    outra) é responsabilidade de `daemon.py`, que ainda não existe — esta
    classe só sabe falar com um pool, uma conexão de cada vez.
    """

    def __init__(
        self,
        host: str,
        port: int,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.host = host
        self.port = port
        self.sleep_func = sleep_func
        self.on_raw_message: Callable[[dict[str, Any]], None] | None = None

        self._sock: socket.socket | None = None
        self._file: TextIO | None = None
        self._next_id = 1

        self.extranonce1: str = ""
        self.extranonce2_size: int = 0
        self.pool_difficulty: float = 1.0
        self.current_job: Job | None = None
        self.accepted_count = 0
        self.rejected_shares: list[tuple[str, str]] = []
        self.connection_state: str = "desconectado"
        """Espelha as transições já logadas, pra SharedState ter de onde ler
        sem duplicar lógica: desconectado, conectando, conectado, reconectando."""

    def connect(self) -> None:
        """Abre a conexão TCP com o pool."""
        self.connection_state = "conectando"
        logger.info("conectando em %s:%s", self.host, self.port)
        self._sock = socket.create_connection((self.host, self.port))
        self._file = self._sock.makefile("rw", encoding="utf-8", newline="\n")
        self.connection_state = "conectado"
        logger.info("conectado em %s:%s", self.host, self.port)

    def close(self) -> None:
        """Fecha a conexão, se estiver aberta."""
        if self._file is not None:
            self._file.close()
        if self._sock is not None:
            self._sock.close()
        self._file = None
        self._sock = None

    def _send(self, method: str, params: list[Any]) -> int:
        msg_id = self._next_id
        self._next_id += 1
        line = json.dumps({"id": msg_id, "method": method, "params": params})
        logger.debug("-> %s", line)
        assert self._file is not None
        self._file.write(line + "\n")
        self._file.flush()
        return msg_id

    def read_message(self) -> dict[str, Any]:
        """Lê e decodifica uma linha JSON-RPC do pool.

        Toda linha recebida passa por `on_raw_message`, se definido, antes
        de qualquer outro processamento — é o ponto único de captura usado
        para gravar a sessão real em `tests/fixtures/public-pool-session.jsonl`.

        Returns:
            O dict decodificado da linha.

        Raises:
            ConnectionError: se o pool fechou a conexão (linha vazia).
        """
        assert self._file is not None
        line = self._file.readline()
        if line == "":
            raise ConnectionError("conexão fechada pelo pool")
        line = line.strip()
        logger.debug("<- %s", line)
        message: dict[str, Any] = json.loads(line)
        if self.on_raw_message is not None:
            self.on_raw_message(message)
        return message

    def subscribe(self, user_agent: str = "quixote/1.0") -> None:
        """Envia `mining.subscribe` e guarda extranonce1/extranonce2_size."""
        self._send("mining.subscribe", [user_agent])
        response = self.read_message()
        result = response["result"]
        self.extranonce1 = result[1]
        self.extranonce2_size = result[2]
        logger.info(
            "subscribed: extranonce1=%s extranonce2_size=%s",
            self.extranonce1,
            self.extranonce2_size,
        )

    def authorize(self, worker: str, password: str = "x") -> None:
        """Envia `mining.authorize`. A senha é ignorada pelo pool."""
        self._send("mining.authorize", [worker, password])
        response = self.read_message()
        if not response.get("result"):
            raise PermissionError(f"autorização recusada: {response}")
        logger.info("autorizado como %s", worker)

    def suggest_difficulty(self, value: float) -> None:
        """Envia `mining.suggest_difficulty`. O pool pode ignorar."""
        self._send("mining.suggest_difficulty", [value])

    def handle_message(self, message: dict[str, Any]) -> None:
        """Despacha uma mensagem já lida: job novo, dificuldade ou resultado de share."""
        method = message.get("method")
        if method == "mining.notify":
            job = Job.from_notify(message["params"])
            logger.info("job novo (clean_jobs=%s): %s", job.clean_jobs, job.job_id)
            self.current_job = job
        elif method == "mining.set_difficulty":
            self.pool_difficulty = message["params"][0]
            logger.info("nova dificuldade da pool: %s", self.pool_difficulty)
        elif "result" in message:
            # só sobra resposta de mining.submit aqui: subscribe/authorize já
            # são consumidos pelos próprios métodos antes do loop de eventos.
            # Confirmado contra o public-pool.io real: rejeição pode vir com
            # "result": false OU "result": null (não só bool).
            if message["result"] is True:
                self.accepted_count += 1
                logger.info("share aceita (total %s)", self.accepted_count)
            else:
                reason = str(message.get("error"))
                self.rejected_shares.append((str(message.get("id")), reason))
                logger.warning("share rejeitada: %s", reason)

    def submit(self, share: Share) -> None:
        """Envia `mining.submit`. A resposta chega depois, no loop de leitura."""
        self._send("mining.submit", share.to_submit_params())

    def run_forever(
        self,
        worker: str,
        password: str = "x",
        difficulty: float = 1.0,
        on_job: Callable[[Job], None] | None = None,
        max_reconnects: int | None = None,
        max_backoff_seconds: float = RECONNECT_BACKOFF_MAX_SECONDS,
    ) -> None:
        """Conecta, autoriza e fica processando mensagens, reconectando quando cai.

        Args:
            worker: `endereco.worker` para `mining.authorize`.
            password: ignorada pelo pool, mas enviada por completude.
            difficulty: valor sugerido em `mining.suggest_difficulty`.
            on_job: chamado com o `Job` atual sempre que chega um `mining.notify`.
            max_reconnects: limite de reconexões para uso em teste; `None`
                roda para sempre (uso real).
            max_backoff_seconds: teto do backoff exponencial de reconexão.
        """
        backoff = RECONNECT_BACKOFF_START_SECONDS
        attempts = 0
        while True:
            try:
                self.connect()
                self.subscribe()
                self.authorize(worker, password)
                self.suggest_difficulty(difficulty)
                backoff = RECONNECT_BACKOFF_START_SECONDS
                if self._sock is not None:
                    self._sock.settimeout(NOTIFY_TIMEOUT_SECONDS)
                while True:
                    message = self.read_message()
                    self.handle_message(message)
                    if (
                        on_job is not None
                        and message.get("method") == "mining.notify"
                        and self.current_job is not None
                    ):
                        on_job(self.current_job)
            except OSError as exc:
                self.connection_state = "reconectando"
                logger.warning("conexão perdida (%s), reconectando em %.0fs", exc, backoff)
                self.close()
                attempts += 1
                if max_reconnects is not None and attempts > max_reconnects:
                    return
                self.sleep_func(backoff)
                backoff = min(backoff * 2, max_backoff_seconds)


if __name__ == "__main__":
    import pathlib

    from quixote.envfile import read_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    env_values = read_env(repo_root)

    address = env_values.get("BTC_ADDRESS")
    if not address:
        raise SystemExit("defina BTC_ADDRESS em .env antes de rodar isto")

    fixture_path = repo_root / "tests" / "fixtures" / "public-pool-session.jsonl"
    start_time = time.monotonic()

    def _grava_e_imprime(msg: dict[str, Any]) -> None:
        with fixture_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.monotonic() - start_time, 3), "raw": msg}) + "\n")

    fixture_path.write_text("")  # começa um arquivo novo a cada captura
    client = StratumClient("public-pool.io", 21496)
    client.on_raw_message = _grava_e_imprime

    notify_count = 0
    deadline = start_time + 90

    def _imprime_job(job: Job) -> None:
        global notify_count
        notify_count += 1
        print(
            f"job {job.job_id}  nbits=0x{job.nbits:08x}  ntime={job.ntime}  "
            f"clean_jobs={job.clean_jobs}  ramos_merkle={len(job.merkle_branch)}"
        )

    client.connect()
    client.subscribe()
    client.authorize(f"{address}.desktop")
    client.suggest_difficulty(1)
    if client._sock is not None:
        client._sock.settimeout(max(1.0, deadline - time.monotonic()))

    try:
        while time.monotonic() < deadline and notify_count < 2:
            msg = client.read_message()
            client.handle_message(msg)
            if msg.get("method") == "mining.notify" and client.current_job is not None:
                _imprime_job(client.current_job)
    finally:
        client.close()

    print(f"captura salva em {fixture_path}")
