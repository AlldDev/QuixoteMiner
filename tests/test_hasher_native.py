"""Conferência de corretude do laço quente em Rust (`quixote_native`) contra
`core.hashing.sha256d` — invariante 2 do `CLAUDE.md` ("endianness primeiro")
aplicada ao lado nativo. Sem `cargo test`: o binário de teste do PyO3 tenta
linkar `libpython` direto, ausente do jeito que o linker espera nesta
máquina — a conferência roda por aqui, contra o `.so` de verdade, que é o
mesmo caminho que `mine_job` usa em produção.
"""

import struct

import quixote_native

from quixote.core.hashing import sha256d


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
