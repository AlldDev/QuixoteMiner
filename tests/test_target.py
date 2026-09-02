"""Testes 7 a 10: target e dificuldade."""

import json
from pathlib import Path

import pytest

from quixote.core.hashing import serialize_header, sha256d
from quixote.core.target import (
    TARGET_DIFF1,
    difficulty_to_target,
    nbits_to_target,
    share_difficulty,
    target_to_difficulty,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_nbits_to_target_bloco_125552():
    """Teste 7."""
    block = _load("block_125552.json")
    nbits = int(block["nbits_hex"], 16)
    assert hex(nbits_to_target(nbits)) == "0x44b9f20000000000000000000000000000000000000000000000"


def test_difficulty_to_target_de_1_e_o_target_diff1():
    """Teste 8."""
    assert difficulty_to_target(1) == TARGET_DIFF1


def test_ida_e_volta_difficulty_target():
    """Teste 9."""
    for x in [1, 2.5, 1000, 244112.48777433642, 1e12]:
        target = difficulty_to_target(x)
        assert target_to_difficulty(target) == pytest.approx(x, rel=1e-6)


def test_dificuldade_do_bloco_125552_maior_que_a_da_rede_na_epoca():
    """Teste 10: a share que virou bloco tem dificuldade maior que a da rede."""
    block = _load("block_125552.json")
    prev_hash = bytes.fromhex(block["prev_hash_display"])[::-1]
    merkle_root = bytes.fromhex(block["merkle_root_display"])[::-1]
    nbits = int(block["nbits_hex"], 16)
    header = serialize_header(
        version=block["version"],
        prev_hash=prev_hash,
        merkle_root=merkle_root,
        ntime=block["ntime"],
        nbits=nbits,
        nonce=block["nonce"],
    )
    header_hash = sha256d(header)
    assert share_difficulty(header_hash) > block["network_difficulty"]
