"""Conferência de corretude do laço quente em Rust (`quixote_native`) contra
`core.hashing.sha256d` — invariante 2 do `CLAUDE.md` ("endianness primeiro")
aplicada ao lado nativo. Sem `cargo test`: o binário de teste do PyO3 tenta
linkar `libpython` direto, ausente do jeito que o linker espera nesta
máquina — a conferência roda por aqui, contra o `.so` de verdade, que é o
mesmo caminho que `mine_job` usa em produção.
"""

import json
import struct
from pathlib import Path

import quixote_native

from quixote.core.hashing import serialize_header, sha256d
from quixote.core.target import nbits_to_target

FIXTURES = Path(__file__).parent / "fixtures"


def _bloco_125552() -> tuple[bytes, int, int]:
    """Header real (prefixo não nulo) pra exercitar o midstate: devolve
    `(header, nonce, nbits)` do exemplo canônico de tests/test_hashing.py."""
    block = json.loads((FIXTURES / "block_125552.json").read_text())
    nbits = int(block["nbits_hex"], 16)
    header = serialize_header(
        version=block["version"],
        prev_hash=bytes.fromhex(block["prev_hash_display"])[::-1],
        merkle_root=bytes.fromhex(block["merkle_root_display"])[::-1],
        ntime=block["ntime"],
        nbits=nbits,
        nonce=block["nonce"],
    )
    return header, block["nonce"], nbits


def test_sha256d_py_bate_com_core_hashing_nos_vetores_triviais():
    """Mesmos vetores de tests/test_hashing.py::test_sha256d_vetores_triviais."""
    assert bytes(quixote_native.sha256d_py(b"")) == sha256d(b"")
    assert bytes(quixote_native.sha256d_py(b"hello")) == sha256d(b"hello")


def test_sha256d_py_bate_em_headers_de_80_bytes_com_nonces_variados():
    header_prefix = b"\x00" * 76
    for nonce in (0, 1, 12345, 2**32 - 1):
        header = header_prefix + struct.pack("<I", nonce)
        assert bytes(quixote_native.sha256d_py(header)) == sha256d(header)


def test_search_nonces_acha_os_mesmos_hashes_que_sha256d_calcularia():
    header_prefix = b"\x00" * 76
    target_pool = ((1 << 256) - 1).to_bytes(32, "little")  # generoso, qualquer hash serve
    target_rede = (0).to_bytes(32, "little")  # nunca bate

    resultado = quixote_native.search_nonces(header_prefix, 0, 10, target_pool, target_rede)

    assert len(resultado) == 10
    for nonce, header_hash in resultado:
        header = header_prefix + struct.pack("<I", nonce)
        assert bytes(header_hash) == sha256d(header)


def test_search_nonces_nao_acha_nada_com_target_impossivel():
    header_prefix = b"\x00" * 76
    target_nulo = (0).to_bytes(32, "little")

    resultado = quixote_native.search_nonces(header_prefix, 0, 100, target_nulo, target_nulo)

    assert resultado == []


def test_search_nonces_com_prefixo_real_bate_com_sha256d_em_todo_nonce():
    header, nonce, _ = _bloco_125552()
    prefixo = header[:76]
    generoso = ((1 << 256) - 1).to_bytes(32, "little")
    nulo = bytes(32)

    resultado = quixote_native.search_nonces(prefixo, nonce - 5, 10, generoso, nulo)

    assert [n for n, _ in resultado] == list(range(nonce - 5, nonce + 5))
    for n, header_hash in resultado:
        assert bytes(header_hash) == sha256d(prefixo + struct.pack("<I", n))


def test_search_nonces_acha_o_bloco_125552_com_o_target_da_rede():
    header, nonce, nbits = _bloco_125552()
    target_rede = nbits_to_target(nbits).to_bytes(32, "little")

    resultado = quixote_native.search_nonces(header[:76], nonce - 5, 10, bytes(32), target_rede)

    assert [(n, bytes(h)) for n, h in resultado] == [(nonce, sha256d(header))]


def test_rejeicao_rapida_empata_na_palavra_alta_e_decide_na_comparacao_completa():
    """Target com a mesma palavra alta (bytes 28..32) do hash: a rejeição
    rápida não pode descartar, quem decide é a comparação dos 32 bytes."""
    header, nonce, _ = _bloco_125552()
    h = int.from_bytes(sha256d(header), "little")

    bate = quixote_native.search_nonces(
        header[:76], nonce, 1, (h + 1).to_bytes(32, "little"), bytes(32)
    )
    nao_bate = quixote_native.search_nonces(
        header[:76], nonce, 1, h.to_bytes(32, "little"), bytes(32)
    )

    assert [n for n, _ in bate] == [nonce]
    assert nao_bate == []


def test_rejeicao_rapida_com_palavra_alta_zero_e_meio_nao_nulo():
    """Target `< 2**224` (palavra alta zero, resto cheio): só pode bater
    hash cuja palavra alta também é zero — conferido contra sha256d."""
    header, nonce, _ = _bloco_125552()
    prefixo = header[:76]
    target = ((1 << 224) - 1).to_bytes(32, "little")
    inicio, quantidade = nonce - 200, 400

    resultado = quixote_native.search_nonces(prefixo, inicio, quantidade, target, bytes(32))

    esperado = [
        n
        for n in range(inicio, inicio + quantidade)
        if int.from_bytes(sha256d(prefixo + struct.pack("<I", n)), "little") < (1 << 224) - 1
    ]
    assert nonce in esperado
    assert [n for n, _ in resultado] == esperado
