"""Conversões entre `nbits`, target e dificuldade.

Duas comparações distintas usam estas funções: a share encontrada contra o
target da pool (dificuldade baixa, deve acontecer com frequência) e contra o
target da rede (dificuldade real, não deve acontecer nunca — se acontecer, é
um bloco de verdade).
"""

from quixote.core.hashing import hash_to_int

TARGET_DIFF1_NBITS = 0x1D00FFFF
"""`nbits` de referência para a dificuldade 1, usado como base de todas as conversões."""


def nbits_to_target(nbits: int) -> int:
    """Expande o formato compacto de 4 bytes para o target de 256 bits.

    Args:
        nbits: dificuldade em formato compacto (1 byte de expoente, 3 bytes
            de coeficiente, lidos do header do bloco).

    Returns:
        O target como inteiro de até 256 bits.
    """
    exponent = nbits >> 24
    coefficient = nbits & 0xFFFFFF
    return coefficient * (1 << (8 * (exponent - 3)))


TARGET_DIFF1 = nbits_to_target(TARGET_DIFF1_NBITS)
"""Target de dificuldade 1, base de `difficulty_to_target` e `target_to_difficulty`."""


def difficulty_to_target(difficulty: float) -> int:
    """Converte uma dificuldade em target.

    Args:
        difficulty: dificuldade, tipicamente a da pool (baixa) ou da rede
            (alta).

    Returns:
        `target = TARGET_DIFF1 / difficulty`, truncado para inteiro.
    """
    return int(TARGET_DIFF1 / difficulty)


def target_to_difficulty(target: int) -> float:
    """Converte um target em dificuldade. Inverso de `difficulty_to_target`.

    Args:
        target: target como inteiro de até 256 bits.

    Returns:
        A dificuldade correspondente.
    """
    return TARGET_DIFF1 / target


def share_difficulty(header_hash: bytes) -> float:
    """Calcula a dificuldade de uma solução encontrada.

    Args:
        header_hash: hash do header, 32 bytes, ordem interna (a mesma que
            sai de `sha256d`).

    Returns:
        A dificuldade equivalente ao hash encontrado, para alimentar o
        campo *best difficulty*.
    """
    return target_to_difficulty(hash_to_int(header_hash))
