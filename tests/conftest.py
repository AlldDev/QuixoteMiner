"""Fábricas compartilhadas pelos testes.

Existe por um motivo só: `BlockCandidate` tem 16 campos (é tudo o que
remonta um header de bloco) e três arquivos de teste precisam de um exemplar
válido. Montar isso à mão em cada um convidaria a divergirem.
"""

import pytest

from quixote.core.hashing import sha256d
from quixote.core.job import BlockCandidate

HEADER_BLOCO_125552 = (
    "01000000"
    "81cd02ab7e569e8bcd9317e2fe99f2de44d49ab2b8851ba4a308000000000000"
    "e320b6c2fffc8d750423db8b1eb942ae710e951ed797f7affc8892b0f1fc122b"
    "c7f5d74d"
    "f2b9441a"
    "42a14695"
)
"""Header real do bloco 125552, o mesmo vetor de `tests/test_hashing.py` —
um candidato de teste que é um bloco de verdade, não bytes inventados."""


@pytest.fixture
def bloco_candidato() -> BlockCandidate:
    """Um `BlockCandidate` coerente: o hash de exibição bate com o header."""
    header = bytes.fromhex(HEADER_BLOCO_125552)
    return BlockCandidate(
        found_at=1_788_000_000.0,
        job_id="job1",
        version=1,
        prev_hash=HEADER_BLOCO_125552[8:72],
        coinb1="01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff08",
        coinb2="ffffffff0100f2052a01000000434104ac00000000",
        merkle_branch=[],
        nbits=0x1A44B9F2,
        ntime=0x4DD7F5C7,
        extranonce1="7fbb156d",
        extranonce2="0000000000000000",
        nonce=0x9546A142,
        coinbase="0100000001" + "00" * 20,
        merkle_root=HEADER_BLOCO_125552[72:136],
        header=HEADER_BLOCO_125552,
        block_hash_display=sha256d(header)[::-1].hex(),
    )
