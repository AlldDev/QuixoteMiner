"""Testes 11 a 14: protocolo Stratum.

O servidor mock roda num socket TCP local de verdade — não é um stub em
memória — porque o que estamos provando é que o cliente fala o protocolo
certo pelo fio, não só que a lógica interna está certa.
"""

import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quixote.core.job import Job, Share, parse_coinbase_height
from quixote.net.stratum import StratumClient

FIXTURES = Path(__file__).parent / "fixtures"

SUBSCRIBE_OK = {"id": 1, "error": None, "result": [[["mining.notify", "aa11bb22"]], "aa11bb22", 4]}
AUTHORIZE_OK = {"id": 2, "error": None, "result": True}


def _load_jsonl(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _synthetic_notify(job_id: str, clean_jobs: bool) -> dict[str, Any]:
    return {
        "id": None,
        "method": "mining.notify",
        "params": [job_id, "00" * 32, "", "", [], "20000000", "17023cc1", "6a970ab8", clean_jobs],
    }


def _start_mock_server(
    handler: Callable[[socket.socket], None],
) -> tuple[threading.Thread, socket.socket, int]:
    """Sobe um servidor TCP local numa thread; o handler decide o roteiro."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(2)
    port = server_sock.getsockname()[1]
    thread = threading.Thread(target=handler, args=(server_sock,), daemon=True)
    thread.start()
    return thread, server_sock, port


def test_handshake_e_job_reais_do_public_pool():
    """Teste 11: cliente manda subscribe/authorize/suggest_difficulty na ordem e formato certos.

    O mock reproduz literalmente uma sessão real gravada em
    ``public-pool-session.jsonl`` (captura contra o public-pool.io):
    respostas de subscribe/authorize e os dois `mining.notify` reais,
    incluindo os 12 ramos de merkle de cada um.
    """
    session = [entry["raw"] for entry in _load_jsonl("public-pool-session.jsonl")]
    received: list[dict[str, Any]] = []

    def handler(server_sock: socket.socket) -> None:
        conn, _ = server_sock.accept()
        f = conn.makefile("rw", encoding="utf-8", newline="\n")
        try:
            for response in session[:2]:  # subscribe, authorize
                received.append(json.loads(f.readline()))
                f.write(json.dumps(response) + "\n")
                f.flush()
            received.append(json.loads(f.readline()))  # suggest_difficulty, sem resposta
            for push in session[2:]:  # set_difficulty, notify, set_difficulty, notify
                f.write(json.dumps(push) + "\n")
                f.flush()
        finally:
            f.close()
            conn.close()
            server_sock.close()

    thread, _server_sock, port = _start_mock_server(handler)
    client = StratumClient("127.0.0.1", port)
    client.connect()
    client.subscribe()
    client.authorize("bc1qexemplo.desktop")
    client.suggest_difficulty(1)
    for _ in session[2:]:
        client.handle_message(client.read_message())
    client.close()
    thread.join(timeout=2)

    assert received[0]["method"] == "mining.subscribe"
    assert received[0]["params"] == ["quixote/1.0"]
    assert received[1]["method"] == "mining.authorize"
    assert received[1]["params"] == ["bc1qexemplo.desktop", "x"]
    assert received[2]["method"] == "mining.suggest_difficulty"
    assert received[2]["params"] == [1]

    assert client.extranonce1 == "7fbb156d"
    assert client.extranonce2_size == 8
    assert client.pool_difficulty == 1
    assert client.current_job is not None
    assert client.current_job.job_id == "6f4961a"
    assert len(client.current_job.merkle_branch) == 12


def test_job_from_notify_com_dados_reais_do_public_pool():
    """Cobertura extra (invariante 'nada sem teste'): parsing de mining.notify real."""
    session = _load_jsonl("public-pool-session.jsonl")
    notify = next(e["raw"] for e in session if e["raw"].get("method") == "mining.notify")
    job = Job.from_notify(notify["params"])
    assert job.job_id == notify["params"][0]
    assert len(job.prev_hash) == 32
    assert len(job.merkle_branch) == 12
    assert all(len(branch) == 32 for branch in job.merkle_branch)
    assert job.clean_jobs is False


def test_parse_coinbase_height_com_dados_reais_do_public_pool():
    """Cobertura extra (BIP34): altura extraída do coinb1 real.

    `coinb1` do job `6f48aac` (mesma sessão real capturada contra o
    public-pool.io): depois de version/txin_count/prevout/script_len, o
    scriptSig começa com um push de 3 bytes (`03`) `c7b90e` — little-endian
    dá 965063, plausível e consistente (o `prevhash` desse mesmo job já
    foi confirmado à parte, contra blockstream.info, como o bloco 965062).
    """
    session = _load_jsonl("public-pool-session.jsonl")
    notify = next(e["raw"] for e in session if e["raw"].get("method") == "mining.notify")
    coinb1 = notify["params"][2]
    assert parse_coinbase_height(coinb1) == 965063


def test_parse_coinbase_height_em_dado_invalido_nao_lanca():
    assert parse_coinbase_height("") is None
    assert parse_coinbase_height("00") is None
    assert parse_coinbase_height("não é hex") is None


def test_clean_jobs_substitui_o_job_atual():
    """Teste 12: clean_jobs=true descarta o job anterior, o atual passa a ser o novo."""

    def handler(server_sock: socket.socket) -> None:
        conn, _ = server_sock.accept()
        f = conn.makefile("rw", encoding="utf-8", newline="\n")
        try:
            f.readline()
            f.write(json.dumps(SUBSCRIBE_OK) + "\n")
            f.flush()
            f.readline()
            f.write(json.dumps(AUTHORIZE_OK) + "\n")
            f.flush()
            f.readline()
            f.write(json.dumps(_synthetic_notify("job1", clean_jobs=False)) + "\n")
            f.flush()
            f.write(json.dumps(_synthetic_notify("job2", clean_jobs=True)) + "\n")
            f.flush()
        finally:
            f.close()
            conn.close()
            server_sock.close()

    thread, _server_sock, port = _start_mock_server(handler)
    client = StratumClient("127.0.0.1", port)
    client.connect()
    client.subscribe()
    client.authorize("worker.desktop")
    client.suggest_difficulty(1)

    client.handle_message(client.read_message())
    assert client.current_job is not None
    assert client.current_job.job_id == "job1"

    client.handle_message(client.read_message())
    assert client.current_job.job_id == "job2"
    assert client.current_job.clean_jobs is True

    client.close()
    thread.join(timeout=2)


def test_reconecta_apos_queda_abrupta_com_backoff():
    """Teste 13: conexão cai sem aviso, cliente reconecta com backoff 1s, 2s..."""

    def handler(server_sock: socket.socket) -> None:
        conn1, _ = server_sock.accept()
        f1 = conn1.makefile("rw", encoding="utf-8", newline="\n")
        f1.readline()
        f1.write(json.dumps(SUBSCRIBE_OK) + "\n")
        f1.flush()
        f1.readline()
        f1.write(json.dumps(AUTHORIZE_OK) + "\n")
        f1.flush()
        f1.readline()
        f1.close()  # sem isso, o socket.close() abaixo não manda FIN de verdade
        conn1.close()  # queda abrupta, sem responder o suggest_difficulty

        conn2, _ = server_sock.accept()
        f2 = conn2.makefile("rw", encoding="utf-8", newline="\n")
        f2.readline()
        f2.write(json.dumps(SUBSCRIBE_OK) + "\n")
        f2.flush()
        f2.readline()
        f2.write(json.dumps(AUTHORIZE_OK) + "\n")
        f2.flush()
        f2.readline()
        f2.write(json.dumps(_synthetic_notify("jobX", clean_jobs=False)) + "\n")
        f2.flush()
        f2.close()
        conn2.close()
        server_sock.close()

    thread, _server_sock, port = _start_mock_server(handler)
    sleeps: list[float] = []
    client = StratumClient("127.0.0.1", port, sleep_func=sleeps.append)

    client.run_forever("worker.teste", max_reconnects=1)

    assert sleeps == [1.0]
    assert client.current_job is not None
    assert client.current_job.job_id == "jobX"
    thread.join(timeout=2)


def test_max_backoff_seconds_customizado_limita_o_crescimento():
    """Cobertura extra (Fase 8, RECONNECT_MAX_BACKOFF): teto de backoff configurável."""

    def handler(server_sock: socket.socket) -> None:
        for _ in range(4):
            conn, _ = server_sock.accept()
            conn.close()  # queda imediata, sem responder nada
        server_sock.close()

    thread, _server_sock, port = _start_mock_server(handler)
    sleeps: list[float] = []
    client = StratumClient("127.0.0.1", port, sleep_func=sleeps.append)

    client.run_forever("worker.teste", max_reconnects=3, max_backoff_seconds=2.0)

    assert sleeps == [1.0, 2.0, 2.0]
    thread.join(timeout=2)


def test_share_rejeitada_registra_motivo_sem_derrubar_conexao():
    """Teste 14: share rejeitada é registrada com o motivo, cliente continua respondendo."""

    def handler(server_sock: socket.socket) -> None:
        conn, _ = server_sock.accept()
        f = conn.makefile("rw", encoding="utf-8", newline="\n")
        try:
            f.readline()
            f.write(json.dumps(SUBSCRIBE_OK) + "\n")
            f.flush()
            f.readline()
            f.write(json.dumps(AUTHORIZE_OK) + "\n")
            f.flush()
            f.readline()
            submit_line = json.loads(f.readline())
            rejection = {
                "id": submit_line["id"],
                "result": False,
                "error": [23, "Low difficulty share", None],
            }
            f.write(json.dumps(rejection) + "\n")
            f.flush()
            f.write(json.dumps(_synthetic_notify("job_depois", clean_jobs=False)) + "\n")
            f.flush()
        finally:
            f.close()
            conn.close()
            server_sock.close()

    thread, _server_sock, port = _start_mock_server(handler)
    client = StratumClient("127.0.0.1", port)
    client.connect()
    client.subscribe()
    client.authorize("worker.desktop")
    client.suggest_difficulty(1)

    share = Share(
        worker="worker.desktop",
        job_id="job1",
        extranonce2=b"\x00\x00\x00\x00",
        ntime=0x6A970AB8,
        nonce=1,
    )
    client.submit(share)
    client.handle_message(client.read_message())

    assert len(client.rejected_shares) == 1
    assert "Low difficulty" in client.rejected_shares[0][1]

    client.handle_message(client.read_message())
    assert client.current_job is not None
    assert client.current_job.job_id == "job_depois"

    client.close()
    thread.join(timeout=2)


def test_submit_sem_conexao_devolve_false_em_vez_de_levantar():
    """`submit` é chamado da thread do hasher, que não é reiniciada por
    ninguém: uma exceção subindo dali parava a mineração em definitivo com o
    processo vivo. `close()` durante a reconexão zera o arquivo, e é
    exatamente a janela em que uma solução pode aparecer."""
    client = StratumClient("127.0.0.1", 1)
    share = Share(worker="w", job_id="job1", extranonce2=b"\x00" * 4, ntime=1, nonce=2)

    assert client.submit(share) is False


def test_só_conta_share_de_submit_que_este_cliente_fez():
    """`accepted_count` é a prova da invariante 1. Antes, qualquer resposta
    com `result: true` e sem `method` entrava na contagem — inclusive a de um
    `mining.suggest_difficulty`, que ninguém consome."""
    client = StratumClient("127.0.0.1", 1)

    client.handle_message({"id": 3, "error": None, "result": True})
    assert client.accepted_count == 0

    client._pending_submits[7] = "job1"
    client.handle_message({"id": 7, "error": None, "result": True})
    assert client.accepted_count == 1

    # e a rejeição registra o job, não o id JSON-RPC
    client._pending_submits[8] = "job2"
    client.handle_message({"id": 8, "error": [23, "Low difficulty share", None], "result": False})
    assert client.rejected_shares[-1][0] == "job2"


def test_subscribe_incrementa_o_epoch_da_sessao():
    """Uma reconexão troca o extranonce1; quem estiver minerando com o antigo
    produz coinbase inválida e precisa parar. O epoch é o sinal."""

    def handler(server_sock: socket.socket) -> None:
        for _ in range(2):
            conn, _addr = server_sock.accept()
            with conn, conn.makefile("rw", encoding="utf-8", newline="\n") as f:
                f.readline()
                f.write(json.dumps(SUBSCRIBE_OK) + "\n")
                f.flush()

    _thread, server_sock, port = _start_mock_server(handler)
    with server_sock:
        client = StratumClient("127.0.0.1", port)
        assert client.session_epoch == 0
        client.connect()
        client.subscribe()
        assert client.session_epoch == 1
        client.close()
        client.connect()
        client.subscribe()
        assert client.session_epoch == 2
        client.close()
