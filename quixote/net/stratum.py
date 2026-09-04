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

    Concorrência é responsabilidade de `daemon.py`, que roda `run_forever`
    numa thread e chama `submit` de outra — esta classe só sabe falar com um
    pool, uma conexão de cada vez.
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
        self.session_epoch = 0
        """Incrementado a cada `subscribe`. Quem estiver minerando precisa
        parar quando isso muda: o `extranonce1` da sessão nova é outro, e a
        coinbase montada com o antigo não confere mais — uma share (ou um
        bloco) enviada depois de uma reconexão com o extranonce velho é
        inválida para o pool."""
        self._pending_submits: dict[int, str] = {}
        """id JSON-RPC → `job_id` das submissões ainda sem resposta. Sem esse
        mapa, `handle_message` contaria como share aceita qualquer resposta
        com `result: true` (a do `mining.suggest_difficulty`, por exemplo) e
        registraria o id no lugar do job em `rejected_shares`."""
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
        # os ids pendentes eram desta sessão; a resposta nunca vai chegar e
        # deixá-los no mapa só acumularia lixo (e o job_id já não valeria mais)
        self._pending_submits.clear()

    def _send(self, method: str, params: list[Any]) -> int:
        msg_id = self._next_id
        self._next_id += 1
        line = json.dumps({"id": msg_id, "method": method, "params": params})
        logger.debug("-> %s", line)
        if self._file is None:
            # acontece de verdade: `close()` zera o arquivo durante a janela de
            # reconexão, e `submit` é chamado da thread do hasher, que não sabe
            # nada disso. ConnectionError (e não AssertionError) porque é uma
            # falha de rede esperada, tratável por quem chamou.
            raise ConnectionError("sem conexão com o pool")
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
        if self._file is None:
            raise ConnectionError("sem conexão com o pool")
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
        self.session_epoch += 1
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
        elif "result" in message and message.get("id") in self._pending_submits:
            # só resposta de submissão que este cliente fez de fato, casada
            # pelo id JSON-RPC. Sem o casamento, a resposta de um
            # `mining.suggest_difficulty` (que ninguém consome) entraria aqui
            # e inflaria `accepted_count`, que é a prova da invariante 1.
            # Confirmado contra o public-pool.io real: rejeição pode vir com
            # "result": false OU "result": null (não só bool).
            job_id = self._pending_submits.pop(message["id"])
            if message["result"] is True:
                self.accepted_count += 1
                # com o job no meio, esta linha sozinha vale como a prova
                # externa da invariante 1: é a resposta a um `mining.submit`
                # que este cliente fez, casada pelo id, e sai em INFO (o
                # `<- {"result":true}` cru só existe em DEBUG)
                logger.info("share do job %s aceita (total %s)", job_id, self.accepted_count)
            else:
                reason = str(message.get("error"))
                self.rejected_shares.append((job_id, reason))
                logger.warning("share do job %s rejeitada: %s", job_id, reason)

    def submit(self, share: Share) -> bool:
        """Envia `mining.submit`. A resposta chega depois, no loop de leitura.

        Nunca levanta por falha de rede: é chamada da thread do hasher, e uma
        exceção subindo dali mataria a mineração em definitivo (a thread não
        é reiniciada por ninguém). Quem chama decide o que fazer com o
        `False` — em share, é só uma share perdida; em bloco, é o evento que
        justifica o registro em disco de `SharedState.record_block_found`.

        Args:
            share: a solução a submeter.

        Returns:
            `True` se a linha foi escrita no socket. Isso não é aceitação: o
            veredito do pool chega depois, em `handle_message`.
        """
        try:
            msg_id = self._send("mining.submit", share.to_submit_params())
        except OSError as exc:
            logger.error("falha ao enviar mining.submit do job %s: %s", share.job_id, exc)
            return False
        self._pending_submits[msg_id] = share.job_id
        return True

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
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                # não só OSError: um JSON quebrado (`JSONDecodeError`, que é
                # `ValueError`) ou um `mining.notify` malformado
                # (`KeyError`/`ValueError`/`TypeError` em `Job.from_notify`,
                # `IndexError` em `set_difficulty` sem params) encerravam esta
                # thread em silêncio, e o daemon seguia mostrando "conectado"
                # sem nunca receber outro job. Reconectar é a resposta certa
                # pros dois casos.
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

    from quixote.core.payout import address_to_script_pubkey
    from quixote.envfile import read_env

    SCRIPT_PUBKEY_PLACEHOLDER = "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    """Vetor de exemplo do BIP173, usado no lugar do `scriptPubKey` real.

    A fixture é versionada, e o `coinb2` que o pool manda carrega o
    `scriptPubKey` do endereço configurado — ou seja, uma captura crua
    colocaria o endereço real do usuário no repositório (invariante 9 do
    CLAUDE.md). A troca é textual e preserva o comprimento, então a fixture
    continua parseável campo a campo."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    env_values = read_env(repo_root)

    address = env_values.get("BTC_ADDRESS")
    if not address:
        raise SystemExit("defina BTC_ADDRESS em .env antes de rodar isto")

    fixture_path = repo_root / "tests" / "fixtures" / "public-pool-session.jsonl"
    start_time = time.monotonic()

    script_pubkey_real = address_to_script_pubkey(address).hex()

    def _grava_e_imprime(msg: dict[str, Any]) -> None:
        linha = json.dumps({"t": round(time.monotonic() - start_time, 3), "raw": msg})
        with fixture_path.open("a", encoding="utf-8") as f:
            f.write(linha.replace(script_pubkey_real, SCRIPT_PUBKEY_PLACEHOLDER) + "\n")

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
