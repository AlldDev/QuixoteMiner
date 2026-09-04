"""Loop de busca de nonce, com throttle opcional.

Só sabe procurar nonce para um `Job` e avisar quando acha algo — não conhece
threads, sockets nem `SharedState`. Quem chama decide o que fazer com o
resultado (montar um `Share` e submeter, por exemplo).

O laço quente (hashear cada nonce) roda em Rust (`quixote_native`,
`rust/quixote_native/`) — única implementação, não um caminho alternativo
opcional. Decisão de 2026-09-02: na máquina de referência, o laço Python
completo sustentava ~800 KH/s e gastava ~46% de um núcleo pros 350 KH/s
travados pela invariante 9; o mesmo em Rust sustenta ~6,5 MH/s (medido de
novo em 2026-09-03: 6.550.746 H/s), o que baixa o custo pra ~11% de um
núcleo. Os números absolutos valem só pra essa máquina — o que vale em
qualquer uma é a razão, e por isso `calibrate_max_hashrate` mede em vez de
assumir. Consequência aceita: `quixote_native` precisa estar compilado
(`maturin develop --release` dentro de `rust/quixote_native/`) pra este
módulo sequer importar — ver `contrib/README.md`.
"""

import logging
import struct
import time
from collections.abc import Callable

import quixote_native

from quixote.core.hashing import hash_to_int, sha256d
from quixote.core.job import BlockCandidate, Job
from quixote.core.merkle import build_coinbase, coinbase_txid, compute_merkle_root, next_extranonce2
from quixote.core.target import nbits_to_target, share_difficulty

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 2000
"""Granularidade do throttle e da checagem de `should_continue` (uma vez
por lote, não mais a cada N nonces — o laço quente inteiro roda em Rust
agora, então a única fronteira onde dá pra checar/dormir é entre lotes)."""

TRACE = 5
"""Nível abaixo de DEBUG (registrado em `quixote.cli`): montagem do header
a cada job — verboso demais pro DEBUG normal."""


def _header_prefix(job: Job, merkle_root: bytes) -> bytes:
    """Os 76 primeiros bytes do header (tudo menos o nonce)."""
    return (
        struct.pack("<I", job.version)
        + job.prev_hash
        + merkle_root
        + struct.pack("<I", job.ntime)
        + struct.pack("<I", job.nbits)
    )


def mine_job(
    job: Job,
    extranonce1: str,
    extranonce2_size: int,
    target_pool: int,
    on_share: Callable[[bytes, int], None],
    should_continue: Callable[[], bool] = lambda: True,
    max_nonces: int = 2**32,
    batch_size: int = DEFAULT_BATCH_SIZE,
    target_hashrate: float | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    on_batch: Callable[[int, float, int], None] | None = None,
    on_share_difficulty: Callable[[float], None] | None = None,
    on_block_found: Callable[[BlockCandidate], None] | None = None,
) -> None:
    """Procura um nonce que satisfaça o target da pool para este job.

    Percorre o espaço de nonce de cada `extranonce2` (2**32 possibilidades),
    incrementando o `extranonce2` sempre que um espaço se esgota sem
    sucesso — na prática isso quase nunca acontece antes de um job novo
    chegar (a 350 KH/s um único extranonce2 dura cerca de 3,4 horas).

    Args:
        job: o job atual, já com merkle_branch e prev_hash na ordem interna.
        extranonce1: extranonce fixo da sessão Stratum, hex.
        extranonce2_size: tamanho em bytes do extranonce2, informado pelo
            pool em `mining.subscribe`.
        target_pool: target da share da pool (dificuldade baixa).
        on_share: chamado com `(extranonce2, nonce)` sempre que um hash
            passa no target da pool.
        should_continue: checado uma vez por lote (`batch_size` nonces) e a
            cada `extranonce2` novo; retornar `False` interrompe a busca
            (job novo chegou, share já foi aceita, etc.).
        max_nonces: limite do espaço de nonce por `extranonce2` — o padrão
            é o espaço real (2**32); testes usam um valor pequeno.
        batch_size: quantos nonces `quixote_native.search_nonces` minera
            por chamada — também a granularidade do throttle (a cada lote,
            mede o tempo e dorme se sobrar). Só importa pro throttle quando
            `target_hashrate` não é `None`.
        target_hashrate: hashrate alvo em H/s. `None` (padrão) desliga o
            throttle por completo — o hasher roda no máximo que conseguir.
        sleep_func: injetável para teste, mesmo padrão de `StratumClient`.
        on_batch: chamado a cada lote com `(hashes_no_lote, segundos,
            nonce_inicial_do_lote)`, pra quem quiser medir hashrate real ou
            mostrar onde a varredura está no espaço de nonce. É o único
            ponto do laço onde o nonce em andamento fica visível de fora.
        on_share_difficulty: chamado com a dificuldade da share (mesmo
            valor do log "share encontrada"), sempre junto de `on_share` —
            evita recalcular o hash de novo só pra saber a dificuldade.
        on_block_found: chamado com o `BlockCandidate` completo sempre que
            um hash passa no target da rede (bloco de verdade, não só share
            de pool) — junto do log CRITICAL "BLOCO ENCONTRADO", e **antes**
            de `on_share` submeter, pra que o registro em disco exista mesmo
            que a submissão exploda. Quem monta e propaga o bloco completo
            pra rede é o pool (o hash já satisfaz o target da pool também,
            então `on_share` dispara do mesmo jeito); o candidato é gravado
            porque a submissão pode falhar e o pool é a única testemunha.
    """
    target_rede = nbits_to_target(job.nbits)
    target_rede_bytes = target_rede.to_bytes(32, "little")
    target_pool_bytes = target_pool.to_bytes(32, "little")
    extranonce2_counter = 0

    while should_continue():
        extranonce2 = next_extranonce2(extranonce2_counter, extranonce2_size)
        coinbase = build_coinbase(job.coinb1, extranonce1, extranonce2.hex(), job.coinb2)
        merkle_root = compute_merkle_root(coinbase_txid(coinbase), job.merkle_branch)
        header_prefix = _header_prefix(job, merkle_root)
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE, "prefixo do header (76 bytes, falta o nonce): %s", header_prefix.hex()
            )

        for start_nonce in range(0, max_nonces, batch_size):
            if not should_continue():
                return
            count = min(batch_size, max_nonces - start_nonce)

            batch_start = time.monotonic()
            matches = quixote_native.search_nonces(
                header_prefix, start_nonce, count, target_pool_bytes, target_rede_bytes
            )
            elapsed = time.monotonic() - batch_start

            for nonce, header_hash_bytes in matches:
                header_hash = bytes(header_hash_bytes)
                value = hash_to_int(header_hash)
                if value < target_rede:
                    header = header_prefix + struct.pack("<I", nonce)
                    candidato = BlockCandidate(
                        found_at=time.time(),
                        job_id=job.job_id,
                        version=job.version,
                        prev_hash=job.prev_hash.hex(),
                        coinb1=job.coinb1,
                        coinb2=job.coinb2,
                        merkle_branch=[branch.hex() for branch in job.merkle_branch],
                        nbits=job.nbits,
                        ntime=job.ntime,
                        extranonce1=extranonce1,
                        extranonce2=extranonce2.hex(),
                        nonce=nonce,
                        coinbase=coinbase.hex(),
                        merkle_root=merkle_root.hex(),
                        header=header.hex(),
                        # o hash de exibição é o reverso do interno; recalcular
                        # a partir do header remontado (em vez de reusar
                        # `header_hash`) confirma que os 80 bytes gravados são
                        # de fato os que produziram o acerto
                        block_hash_display=sha256d(header)[::-1].hex(),
                    )
                    logger.critical(
                        "BLOCO ENCONTRADO! job=%s extranonce2=%s nonce=%s hash=%s",
                        job.job_id,
                        extranonce2.hex(),
                        nonce,
                        candidato.block_hash_display,
                    )
                    if on_block_found is not None:
                        on_block_found(candidato)
                if value < target_pool:
                    difficulty = share_difficulty(header_hash)
                    logger.info("share encontrada: job=%s dificuldade=%.4f", job.job_id, difficulty)
                    on_share(extranonce2, nonce)
                    if on_share_difficulty is not None:
                        on_share_difficulty(difficulty)

            if target_hashrate:
                due = count / target_hashrate
                if due > elapsed:
                    sleep_func(due - elapsed)
                    elapsed = due
            if on_batch is not None:
                on_batch(count, elapsed, start_nonce)

        extranonce2_counter += 1


def calibrate_max_hashrate(duration_seconds: float = 3.0) -> float:
    """Mede a capacidade máxima de hash da máquina, sem throttle nenhum.

    Rodado uma vez na primeira execução do daemon, pra calibrar o quanto
    de CPU o throttle vai usar pra sustentar o hashrate-alvo. Martela
    `quixote_native.search_nonces` (o mesmo caminho que `mine_job` usa de
    verdade) em vez de medir `sha256d` isolado — senão a capacidade
    calibrada não refletiria o que o daemon de fato executa.

    Args:
        duration_seconds: por quanto tempo martelar `search_nonces`.

    Returns:
        Hashes por segundo medidos.
    """
    header_prefix = b"\x00" * 76
    target_nulo = (0).to_bytes(32, "little")  # nunca bate, não queremos achar nada aqui
    count = 0
    nonce = 0
    start = time.monotonic()
    deadline = start + duration_seconds
    while time.monotonic() < deadline:
        quixote_native.search_nonces(
            header_prefix, nonce, DEFAULT_BATCH_SIZE, target_nulo, target_nulo
        )
        count += DEFAULT_BATCH_SIZE
        nonce += DEFAULT_BATCH_SIZE
    elapsed = time.monotonic() - start
    return count / elapsed


def check_target_reachable(target_hashrate: float, calibrated_max_hashrate: float) -> None:
    """Avisa se o hashrate configurado passa da capacidade calibrada da máquina.

    Não impede nada: o próprio throttle em `mine_job` já roda no máximo
    possível sozinho quando um lote demora mais que o tempo devido.

    Args:
        target_hashrate: hashrate configurado (`.env` ou padrão).
        calibrated_max_hashrate: resultado de `calibrate_max_hashrate`.
    """
    if target_hashrate > calibrated_max_hashrate:
        logger.warning(
            "alvo de %.0f H/s inatingível nesta máquina (capacidade medida: %.0f H/s); "
            "vai rodar no máximo possível",
            target_hashrate,
            calibrated_max_hashrate,
        )
