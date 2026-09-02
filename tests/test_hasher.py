"""Cobertura de core/hasher.py (invariante 'nada sem teste')."""

import logging
import struct

import quixote.core.hasher as hasher_module
from quixote.core.hasher import mine_job
from quixote.core.hashing import hash_to_int, sha256d
from quixote.core.job import Job
from quixote.core.merkle import build_coinbase, coinbase_txid, compute_merkle_root, next_extranonce2

EXTRANONCE1 = "aa11bb22"
EXTRANONCE2_SIZE = 4
NBITS_QUALQUER_HASH_SATISFAZ = (
    0x20FFFFFF  # target = coeficiente << 232, maior que qualquer hash real
)


def _job(nbits: int = 0x1D00FFFF) -> Job:
    return Job(
        job_id="job1",
        prev_hash=b"\x00" * 32,
        coinb1="01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff08",
        coinb2="ffffffff0100f2052a01000000434104ac00000000",
        merkle_branch=[],
        version=1,
        nbits=nbits,
        ntime=1305998791,
        clean_jobs=False,
    )


def _stop_after(n_calls: int):
    count = {"n": 0}

    def _check() -> bool:
        count["n"] += 1
        return count["n"] <= n_calls

    return _check


def test_encontra_share_com_target_generoso():
    """target_pool = 2**256-1: qualquer hash satisfaz, o primeiro nonce já dispara on_share."""
    job = _job()
    found: list[tuple[bytes, int]] = []

    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=2**256 - 1,
        on_share=lambda extranonce2, nonce: found.append((extranonce2, nonce)),
        should_continue=_stop_after(2),  # 1 chamada na entrada do while, 1 na checagem do nonce=0
        max_nonces=1,
    )

    assert len(found) == 1
    extranonce2, nonce = found[0]
    assert extranonce2 == next_extranonce2(0, EXTRANONCE2_SIZE)
    assert nonce == 0

    # reconstrói o header por fora e confere que o hash bate com o target
    coinbase = build_coinbase(job.coinb1, EXTRANONCE1, extranonce2.hex(), job.coinb2)
    merkle_root = compute_merkle_root(coinbase_txid(coinbase), job.merkle_branch)
    header = (
        struct.pack("<I", job.version)
        + job.prev_hash
        + merkle_root
        + struct.pack("<I", job.ntime)
        + struct.pack("<I", job.nbits)
        + struct.pack("<I", nonce)
    )
    assert hash_to_int(sha256d(header)) < 2**256 - 1


def test_nao_acha_nada_com_target_impossivel_e_avanca_extranonce2(monkeypatch):
    """target_pool=0 nunca é satisfeito; o extranonce2 deve avançar a cada esgotamento do nonce."""
    job = _job()
    tried_extranonce2: list[bytes] = []

    def _spy(counter: int, size: int) -> bytes:
        value = next_extranonce2(counter, size)
        tried_extranonce2.append(value)
        return value

    monkeypatch.setattr(hasher_module, "next_extranonce2", _spy)

    found: list[tuple[bytes, int]] = []
    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=0,
        on_share=lambda extranonce2, nonce: found.append((extranonce2, nonce)),
        should_continue=_stop_after(3),  # deixa passar por 2 extranonce2 completos
        max_nonces=5,
    )

    assert found == []
    assert tried_extranonce2[:2] == [
        next_extranonce2(0, EXTRANONCE2_SIZE),
        next_extranonce2(1, EXTRANONCE2_SIZE),
    ]


def test_should_continue_falso_de_cara_nao_acha_nada():
    job = _job()
    found: list[tuple[bytes, int]] = []

    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=2**256 - 1,
        on_share=lambda extranonce2, nonce: found.append((extranonce2, nonce)),
        should_continue=lambda: False,
        max_nonces=1000,
    )

    assert found == []


def test_on_extranonce2_change_chamado_a_cada_iteracao_do_laco_externo():
    """Cobertura extra: extranonce2 atual reportado, primeiro incluso."""
    job = _job()
    vistos: list[bytes] = []

    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=0,  # nunca satisfeito, força esgotar o espaço de nonce e avançar
        on_share=lambda extranonce2, nonce: None,
        should_continue=_stop_after(3),  # deixa passar por 2 extranonce2 completos
        max_nonces=5,
        on_extranonce2_change=vistos.append,
    )

    assert vistos == [
        next_extranonce2(0, EXTRANONCE2_SIZE),
        next_extranonce2(1, EXTRANONCE2_SIZE),
    ]


def test_trace_loga_prefixo_do_header(caplog):
    """Cobertura extra (nível TRACE, `quixote.cli.TRACE`).

    Só o prefixo do header (por `extranonce2`) — o TRACE de "primeiro hash
    do lote" existia pra inspecionar hash a hash dentro do laço Python, que
    não existe mais (o laço quente roda em Rust desde a migração de
    2026-09-02, `quixote_native`)."""
    job = _job()

    with caplog.at_level(5, logger="quixote.core.hasher"):
        mine_job(
            job,
            EXTRANONCE1,
            EXTRANONCE2_SIZE,
            target_pool=0,  # nunca satisfeito, só queremos o log de TRACE
            on_share=lambda extranonce2, nonce: None,
            should_continue=_stop_after(2),
            max_nonces=5,
        )

    mensagens = [record.message for record in caplog.records]
    assert any("prefixo do header" in m for m in mensagens)


def test_loga_critical_quando_passa_no_target_da_rede(caplog):
    """Hash abaixo do target da rede é bloco de verdade, log CRITICAL."""
    job = _job(nbits=NBITS_QUALQUER_HASH_SATISFAZ)

    with caplog.at_level(logging.CRITICAL, logger="quixote.core.hasher"):
        mine_job(
            job,
            EXTRANONCE1,
            EXTRANONCE2_SIZE,
            target_pool=2**256 - 1,
            on_share=lambda extranonce2, nonce: None,
            should_continue=_stop_after(2),
            max_nonces=1,
        )

    assert any("BLOCO ENCONTRADO" in record.message for record in caplog.records)


def test_chama_on_block_found_quando_passa_no_target_da_rede():
    """`on_block_found` dispara junto do log CRITICAL — não só o log fica
    sabendo, quem chamou `mine_job` (o daemon, pra telemetria) também."""
    job = _job(nbits=NBITS_QUALQUER_HASH_SATISFAZ)
    chamadas = {"n": 0}

    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=2**256 - 1,
        on_share=lambda extranonce2, nonce: None,
        should_continue=_stop_after(2),
        max_nonces=1,
        on_block_found=lambda: chamadas.__setitem__("n", chamadas["n"] + 1),
    )

    assert chamadas["n"] == 1
