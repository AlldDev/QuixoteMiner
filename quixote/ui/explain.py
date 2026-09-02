"""Modo didático: explica passo a passo a montagem do primeiro job recebido.

`--explain` chama `explicar_job` uma única vez, pro primeiro job — depois
disso o daemon segue minerando normalmente. O nível `TRACE` (`quixote.cli`)
cobre o equivalente contínuo, pra todo job seguinte.
"""

import sys

from quixote.core.hashing import serialize_header, sha256d
from quixote.core.job import Job
from quixote.core.merkle import (
    build_coinbase,
    coinbase_txid,
    compute_merkle_root,
    next_extranonce2,
)
from quixote.core.target import difficulty_to_target, nbits_to_target, target_to_difficulty


def explicar_job(
    job: Job,
    extranonce1: str,
    extranonce2_size: int,
    pool_difficulty: float,
    target_hashrate: float,
    batch_size: int,
    calibrated_max_hashrate: float | None,
) -> None:
    """Imprime a montagem completa do header pra este job, campo a campo.

    Reaproveita as mesmas funções puras que o hasher usa de verdade
    (`build_coinbase`, `coinbase_txid`, `compute_merkle_root`,
    `serialize_header`) — o que aparece aqui é literalmente o que seria
    hasheado, não uma reconstrução manual à parte. O laço de exibição do
    merkle (abaixo) replica o fold de `compute_merkle_root` só pra
    imprimir cada passo; o teste confere que os dois caminhos batem.
    """
    extranonce2 = next_extranonce2(0, extranonce2_size)
    coinbase = build_coinbase(job.coinb1, extranonce1, extranonce2.hex(), job.coinb2)
    txid = coinbase_txid(coinbase)
    merkle_root = compute_merkle_root(txid, job.merkle_branch)

    print("=" * 78)
    print(f"EXPLICAÇÃO DO JOB {job.job_id}")
    print("=" * 78)

    print("\n--- COINBASE E MERKLE ---")
    print(f"coinbase montada ({len(coinbase)} bytes): {coinbase.hex()}")
    print(f"txid da coinbase (ordem interna, a mesma usada no hash): {txid.hex()}")
    node = txid
    for indice, branch in enumerate(job.merkle_branch, start=1):
        node = sha256d(node + branch)
        print(f"  passo {indice}: sha256d(node + ramo {branch.hex()}) = {node.hex()}")
    print(f"merkle root final (invertido, formato de explorador): {merkle_root[::-1].hex()}")

    print("\n--- HEADER (80 bytes: offset, campo, bytes, valor) ---")
    header = serialize_header(
        job.version, job.prev_hash, merkle_root, job.ntime, job.nbits, nonce=0
    )
    campos = [
        (0, 4, "version", f"{job.version} (0x{job.version:08x})"),
        (4, 32, "prev_block_hash", job.prev_hash[::-1].hex()),
        (36, 32, "merkle_root", merkle_root[::-1].hex()),
        (68, 4, "ntime", f"{job.ntime} (unix timestamp)"),
        (72, 4, "nbits", f"0x{job.nbits:08x}"),
        (76, 4, "nonce", "variável — é o que o hasher percorre, de 0 a 2**32-1"),
    ]
    for offset, tamanho, nome, valor in campos:
        bytes_campo = header[offset : offset + tamanho].hex()
        print(f"  [{offset:>2}:{offset + tamanho:<2}] {nome:<16} {bytes_campo:<64} {valor}")

    print("\n--- TARGET EXPANDIDO ---")
    target_rede = nbits_to_target(job.nbits)
    target_pool = difficulty_to_target(pool_difficulty)
    network_difficulty = target_to_difficulty(target_rede)
    print(f"nbits 0x{job.nbits:08x} -> target da rede: {target_rede:#066x}")
    print(f"  dificuldade da rede: {network_difficulty:,.2f}")
    print(f"dificuldade da pool {pool_difficulty} -> target da pool: {target_pool:#066x}")
    print(
        "  o hash do header precisa ficar ABAIXO do target da pool pra virar uma\n"
        "  share (frequente), e abaixo do target da rede — bem mais raro — pra\n"
        "  virar um bloco de verdade."
    )

    print("\n--- CÁLCULO DO THROTTLE ---")
    print(f"hashrate-alvo: {target_hashrate:,.0f} H/s")
    print(f"tamanho do lote: {batch_size} hashes")
    print(f"tempo esperado por lote: {batch_size / target_hashrate:.4f}s (dorme o restante)")
    if calibrated_max_hashrate:
        percentual = target_hashrate / calibrated_max_hashrate * 100
        print(f"capacidade calibrada desta máquina: {calibrated_max_hashrate:,.0f} H/s")
        print(f"  o alvo usa {percentual:.1f}% dessa capacidade")
    print("=" * 78 + "\n")
    # stdout fica bufferizado em bloco quando não é um TTY (systemd/journald,
    # ou redirecionado a um arquivo) — sem o flush, esta explicação só
    # apareceria quando o processo saísse, não quando o job chegasse de fato.
    sys.stdout.flush()
