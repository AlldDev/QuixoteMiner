"""Testes 4 a 6: merkle e coinbase."""

import json
from pathlib import Path

from quixote.core.hashing import sha256d
from quixote.core.merkle import (
    build_coinbase,
    coinbase_txid,
    compute_merkle_root,
    next_extranonce2,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_merkle_root_bloco_100000_a_partir_dos_txids():
    """Teste 4: merkle root das 4 transações reais do bloco 100000."""
    block = _load("block_100000.json")
    nivel = [bytes.fromhex(t)[::-1] for t in block["txids_display"]]
    while len(nivel) > 1:
        if len(nivel) % 2 == 1:
            nivel.append(nivel[-1])
        nivel = [sha256d(nivel[i] + nivel[i + 1]) for i in range(0, len(nivel), 2)]
    root = nivel[0]
    assert root[::-1].hex() == block["merkle_root_display"]


def test_merkle_root_com_unica_transacao():
    """Teste 5: com uma única transação, o root é igual ao próprio txid."""
    txid = sha256d(b"transacao unica qualquer")
    assert compute_merkle_root(txid, []) == txid


def test_reconstrucao_coinbase_e_merkle_bloco_100000():
    """Teste 6: build_coinbase + compute_merkle_root reproduzem o merkle root
    e o hash reais do bloco 100000, a partir da coinbase real recortada
    (ver nota em fixtures/block_100000.json sobre a origem do recorte)."""
    block = _load("block_100000.json")
    split = block["coinbase_split"]

    coinbase = build_coinbase(
        split["coinb1"], split["extranonce1"], split["extranonce2"], split["coinb2"]
    )
    assert coinbase == bytes.fromhex(block["coinbase_raw_hex"])

    cb_txid = coinbase_txid(coinbase)
    assert cb_txid[::-1].hex() == block["txids_display"][0]

    outros = [bytes.fromhex(t)[::-1] for t in block["txids_display"][1:]]
    h1, h2, h3 = outros
    p1 = sha256d(h2 + h3)

    root = compute_merkle_root(cb_txid, [h1, p1])
    assert root[::-1].hex() == block["merkle_root_display"]


def test_next_extranonce2_empacota_no_tamanho_do_pool():
    assert next_extranonce2(0, 4) == b"\x00\x00\x00\x00"
    assert next_extranonce2(1, 4) == b"\x00\x00\x00\x01"
    assert next_extranonce2(256, 2) == b"\x01\x00"


def test_compute_merkle_root_loga_cada_passo_em_trace(caplog):
    """Cobertura extra (nível TRACE, `quixote.cli.TRACE`): passo a passo do merkle."""
    txid = sha256d(b"coinbase")
    branch = sha256d(b"irmao")

    with caplog.at_level(5, logger="quixote.core.merkle"):
        resultado = compute_merkle_root(txid, [branch])

    assert any(txid.hex() in record.message for record in caplog.records)
    assert any(branch.hex() in record.message for record in caplog.records)
    assert any(resultado.hex() in record.message for record in caplog.records)
