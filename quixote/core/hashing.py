"""Primitiva criptográfica e montagem do header de 80 bytes do bloco.

Convenção adotada no projeto inteiro: hashes de bloco e merkle root ficam
guardados internamente na ordem de bytes que o header usa para ser hasheado
(little-endian "corrida", a mesma que sai direto de ``sha256d``). Essa ordem é
o reverso byte a byte do hexadecimal que exploradores de bloco e o protocolo
Stratum mostram para humanos. A inversão só acontece na borda, ao exibir ou ao
ler um valor que veio em formato de exibição.
"""

import hashlib
import struct

HEADER_SIZE_BYTES = 80


def sha256d(data: bytes | bytearray) -> bytes:
    """Calcula o double SHA-256 usado em todo o protocolo Bitcoin.

    Aceita ``bytearray`` além de ``bytes`` porque o laço quente de mineração
    (``core.hasher.mine_job``) reaproveita um buffer mutável em vez de montar
    um ``bytes`` novo a cada nonce — ``hashlib`` já aceita os dois em tempo de
    execução, isso só destrava o `mypy --strict`.

    Args:
        data: bytes de entrada, em qualquer ordem (a função é agnóstica).

    Returns:
        ``sha256(sha256(data))``, 32 bytes.
    """
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def serialize_header(
    version: int,
    prev_hash: bytes,
    merkle_root: bytes,
    ntime: int,
    nbits: int,
    nonce: int,
) -> bytes:
    """Monta os 80 bytes exatos do header de bloco.

    Args:
        version: versão do bloco.
        prev_hash: hash do bloco anterior, 32 bytes, já na ordem interna
            (little-endian corrida, isto é, o reverso do hex de exibição).
        merkle_root: merkle root, 32 bytes, mesma ordem interna de prev_hash.
        ntime: timestamp Unix do bloco.
        nbits: dificuldade em formato compacto.
        nonce: nonce do header.

    Returns:
        Os 80 bytes do header, prontos para ``sha256d``.
    """
    if len(prev_hash) != 32 or len(merkle_root) != 32:
        raise ValueError("prev_hash e merkle_root precisam ter 32 bytes")
    return (
        struct.pack("<I", version)
        + prev_hash
        + merkle_root
        + struct.pack("<I", ntime)
        + struct.pack("<I", nbits)
        + struct.pack("<I", nonce)
    )


def swap_endian_words(hex_str: str) -> str:
    """Inverte a ordem de bytes dentro de cada palavra de 4 bytes.

    O `prevhash` que o Stratum manda em `mining.notify` vem como 8 palavras
    de 4 bytes, cada palavra com os bytes trocados em relação à ordem que o
    header espera. Aplicar esta função converte de um formato para o outro
    (a operação é sua própria inversa).

    Args:
        hex_str: string hexadecimal cujo comprimento é múltiplo de 8
            caracteres (múltiplo de 4 bytes).

    Returns:
        A mesma quantidade de palavras, cada uma com os bytes internos
        invertidos, na mesma ordem de palavras da entrada.
    """
    if len(hex_str) % 8 != 0:
        raise ValueError("hex_str precisa ter comprimento múltiplo de 8")
    words = [hex_str[i : i + 8] for i in range(0, len(hex_str), 8)]
    return "".join(bytes.fromhex(word)[::-1].hex() for word in words)


def hash_to_int(h: bytes) -> int:
    """Interpreta um hash como inteiro little-endian, para comparar com target.

    Args:
        h: hash de 32 bytes, na ordem interna (a mesma que sai de sha256d).

    Returns:
        O valor inteiro correspondente, pronto para comparar com um target.
    """
    return int.from_bytes(h, "little")
