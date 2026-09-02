"""Testes 1 a 3: vetores conhecidos de hashing."""

import json
from pathlib import Path

from quixote.core.hashing import hash_to_int, serialize_header, sha256d, swap_endian_words

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _header_bytes_from_fixture(block: dict) -> bytes:
    prev_hash = bytes.fromhex(block["prev_hash_display"])[::-1]
    merkle_root = bytes.fromhex(block["merkle_root_display"])[::-1]
    nbits = int(block["nbits_hex"], 16)
    return serialize_header(
        version=block["version"],
        prev_hash=prev_hash,
        merkle_root=merkle_root,
        ntime=block["ntime"],
        nbits=nbits,
        nonce=block["nonce"],
    )


def test_bloco_125552():
    """Teste 1: o exemplo canônico da wiki do Bitcoin."""
    block = _load("block_125552.json")
    header = _header_bytes_from_fixture(block)
    assert len(header) == 80
    resultado = sha256d(header)[::-1].hex()
    assert resultado == block["block_hash_display"]


def test_bloco_100000():
    """Teste 2: segundo vetor independente."""
    block = _load("block_100000.json")
    header = _header_bytes_from_fixture(block)
    resultado = sha256d(header)[::-1].hex()
    assert resultado == block["block_hash_display"]


def test_sha256d_vetores_triviais():
    """Teste 3: sha256d contra vetores fixos, hash duplo bem conhecido do vazio e de "hello"."""
    assert sha256d(b"").hex() == "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456"
    assert (
        sha256d(b"hello").hex()
        == "9595c9df90075148eb06860365df33584b75bff782a510c6cd4883a419833d50"
    )


def test_swap_endian_words_e_involutiva():
    original = "aabbccdd11223344"
    trocado = swap_endian_words(original)
    assert trocado == "ddccbbaa44332211"
    assert swap_endian_words(trocado) == original


def test_hash_to_int_le():
    assert hash_to_int(b"\x01\x00") == 1
    assert hash_to_int(b"\x00\x01") == 256
