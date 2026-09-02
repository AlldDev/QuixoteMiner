"""Teste 22: a prova de aceitação — mineração real, share aceita pelo pool.

Conecta de verdade no public-pool.io, minera sem throttle e espera a
primeira share ser aceita. `@pytest.mark.slow` porque pode levar minutos —
rodar manualmente com `pytest -v tests/test_integration_pool.py`.

A coordenação entre a thread do Stratum e o loop do hasher aqui é uma
versão simplificada, só pra este teste — o `_JobBox` sem lock (o próprio
GIL cobre a troca de referência de um job por outro) e sem `SharedState`.
O `daemon.py` de produção usa o mesmo padrão de versão de job, mas com
`SharedState` protegido por `threading.Lock` de verdade, porque lá há mais
de uma thread lendo/escrevendo o mesmo estado (Stratum, Hasher, IPC).
"""

import logging
import pathlib
import threading
import time

import pytest

from quixote.core.hasher import mine_job
from quixote.core.job import Job, Share
from quixote.core.target import difficulty_to_target
from quixote.envfile import read_env
from quixote.net.stratum import StratumClient

TIMEOUT_SECONDS = 30 * 60


def _read_btc_address() -> str | None:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return read_env(repo_root).get("BTC_ADDRESS") or None


class _JobBox:
    """Guarda o job mais recente e uma versão, pra o hasher saber quando parar.

    A versão só avança em `clean_jobs=True` (ou no primeiro job): quando
    `clean_jobs` é verdadeiro, o trabalho em andamento deve ser descartado
    imediatamente. Sem esse filtro, o hasher reiniciava a cada
    `mining.notify`, e na prática o public-pool manda um a cada ~30s
    (mempool mudando) contra só 1 a cada alguns minutos com
    `clean_jobs=true` de verdade — reiniciar à toa jogava fora o
    header/merkle já montados sem necessidade.
    """

    def __init__(self) -> None:
        self.job: Job | None = None
        self.version = 0

    def set(self, job: Job) -> None:
        self.job = job
        if job.clean_jobs or self.version == 0:
            self.version += 1


@pytest.mark.slow
def test_primeira_share_aceita_pelo_public_pool(caplog):
    address = _read_btc_address()
    if not address:
        pytest.skip("defina BTC_ADDRESS em .env pra rodar o teste de integração")

    worker = f"{address}.desktop"
    client = StratumClient("public-pool.io", 21496)
    job_box = _JobBox()

    thread = threading.Thread(
        target=client.run_forever,
        kwargs={"worker": worker, "difficulty": 1, "on_job": job_box.set},
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + TIMEOUT_SECONDS

    with caplog.at_level(logging.DEBUG):
        while client.accepted_count == 0 and time.monotonic() < deadline:
            job = job_box.job
            if job is None:
                time.sleep(0.2)
                continue

            version = job_box.version
            target_pool = difficulty_to_target(client.pool_difficulty)

            def _submit(extranonce2: bytes, nonce: int, _job: Job = job) -> None:
                share = Share(
                    worker=worker,
                    job_id=_job.job_id,
                    extranonce2=extranonce2,
                    ntime=_job.ntime,
                    nonce=nonce,
                )
                client.submit(share)

            mine_job(
                job,
                client.extranonce1,
                client.extranonce2_size,
                target_pool,
                on_share=_submit,
                should_continue=(
                    lambda v=version: (
                        job_box.version == v
                        and client.accepted_count == 0
                        and time.monotonic() < deadline
                    )
                ),
            )

    assert client.accepted_count > 0, (
        f"nenhuma share aceita em {TIMEOUT_SECONDS}s; rejeitadas: {client.rejected_shares}"
    )
    assert any('"result":true' in record.message.replace(" ", "") for record in caplog.records), (
        "share aceita mas a linha crua com result:true não apareceu no log DEBUG"
    )
