"""Montagem da coinbase e cálculo do merkle root.

Mesma convenção de `core.hashing`: txids e merkle root ficam na ordem
interna (little-endian corrida), reverso do hex de exibição.
"""

import logging

from quixote.core.hashing import sha256d

logger = logging.getLogger(__name__)


def build_coinbase(coinb1: str, extranonce1: str, extranonce2: str, coinb2: str) -> bytes:
    """Concatena as quatro partes da transação coinbase.

    Args:
        coinb1: primeira metade da coinbase, hex, vinda do pool.
        extranonce1: extranonce fixo desta conexão, hex, vindo do pool.
        extranonce2: extranonce variável, hex, gerado localmente.
        coinb2: segunda metade da coinbase, hex, vinda do pool.

    Returns:
        Bytes da transação coinbase completa.
    """
    return bytes.fromhex(coinb1 + extranonce1 + extranonce2 + coinb2)


def coinbase_txid(coinbase: bytes) -> bytes:
    """Calcula o txid da coinbase completa.

    Args:
        coinbase: bytes da transação coinbase montada por `build_coinbase`.

    Returns:
        `sha256d(coinbase)`, 32 bytes, ordem interna.
    """
    return sha256d(coinbase)


def compute_merkle_root(coinbase_txid: bytes, branches: list[bytes]) -> bytes:
    """Sobe a árvore de merkle a partir do txid da coinbase.

    A coinbase é sempre a primeira transação do bloco, então em cada nível
    ela (ou o nó acumulado) é sempre concatenada à esquerda do galho.

    Args:
        coinbase_txid: txid da coinbase, 32 bytes, ordem interna.
        branches: lista de hashes irmãos, 32 bytes cada, ordem interna, na
            ordem em que o pool os manda em `mining.notify`.

    Returns:
        O merkle root, 32 bytes, ordem interna.
    """
    node = coinbase_txid
    for branch in branches:
        novo_node = sha256d(node + branch)
        if logger.isEnabledFor(5):  # TRACE, ver quixote.cli
            logger.log(5, "merkle: %s + %s -> %s", node.hex(), branch.hex(), novo_node.hex())
        node = novo_node
    return node


def next_extranonce2(counter: int, size: int) -> bytes:
    """Empacota o contador de extranonce2 no tamanho que o pool informou.

    Args:
        counter: valor incremental do contador, reiniciado a cada job novo.
        size: tamanho em bytes que `mining.subscribe` informou.

    Returns:
        `counter` empacotado em `size` bytes, big-endian (é assim que o
        Stratum espera o campo `extranonce2` em `mining.submit`).
    """
    return counter.to_bytes(size, "big")
