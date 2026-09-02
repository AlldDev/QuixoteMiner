"""Testes 15 a 18: throttle."""

import logging
import time

from quixote.core.hasher import calibrate_max_hashrate, check_target_reachable, mine_job
from quixote.core.job import Job

EXTRANONCE1 = "aa11bb22"
EXTRANONCE2_SIZE = 4


def _job() -> Job:
    return Job(
        job_id="job1",
        prev_hash=b"\x00" * 32,
        coinb1="01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff08",
        coinb2="ffffffff0100f2052a01000000434104ac00000000",
        merkle_branch=[],
        version=1,
        nbits=0x1D00FFFF,
        ntime=1305998791,
        clean_jobs=False,
    )


def _mine_por_segundos(target_hashrate: float | None, segundos: float) -> tuple[int, float]:
    """Roda mine_job throttled por `segundos` reais, devolve (hashes_totais, tempo_real)."""
    job = _job()
    total = {"hashes": 0}
    inicio = time.monotonic()
    deadline = inicio + segundos

    def _on_batch(hashes_no_lote: int, _elapsed: float) -> None:
        total["hashes"] += hashes_no_lote

    mine_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        target_pool=0,  # impossível, não queremos achar share nesse teste
        on_share=lambda extranonce2, nonce: None,
        should_continue=lambda: time.monotonic() < deadline,
        target_hashrate=target_hashrate,
        on_batch=_on_batch,
    )
    tempo_real = time.monotonic() - inicio
    return total["hashes"], tempo_real


def test_hashrate_100khz_dentro_de_5_por_cento():
    """Teste 15."""
    alvo = 100_000.0
    hashes, tempo_real = _mine_por_segundos(alvo, 10.0)
    medido = hashes / tempo_real
    assert abs(medido - alvo) / alvo < 0.05


def test_hashrate_350khz_dentro_de_5_por_cento():
    """Teste 16."""
    alvo = 350_000.0
    hashes, tempo_real = _mine_por_segundos(alvo, 10.0)
    medido = hashes / tempo_real
    assert abs(medido - alvo) / alvo < 0.05


def test_alvo_inatingivel_loga_aviso_e_nao_trava(caplog):
    """Teste 17."""
    teto = calibrate_max_hashrate(duration_seconds=0.3)
    alvo_inatingivel = teto * 10

    with caplog.at_level(logging.WARNING, logger="quixote.core.hasher"):
        check_target_reachable(alvo_inatingivel, teto)

    assert any("inatingível" in record.message for record in caplog.records)

    # roda de verdade por um tempo curto: não pode travar nem lançar exceção
    hashes, tempo_real = _mine_por_segundos(alvo_inatingivel, 1.0)
    assert hashes > 0
    assert tempo_real < 3.0  # bem acima de 1s já indicaria travamento


def test_cpu_proporcional_a_capacidade_calibrada_com_alvo_350khz():
    """Teste 18, adaptado a esta máquina e, desde a migração pra Rust
    (`quixote_native`, 2026-09-02), a uma capacidade calibrada bem maior.

    Antes da migração, sustentar 350 KH/s custava ~44% de um núcleo nesta
    máquina — a fração de CPU seguia de perto `alvo /
    capacidade_efetiva_calibrada`. Com o laço quente em Rust
    (capacidade medida ~6-7 MH/s), o mesmo alvo passa a exigir uma fração
    bem menor (~5%) — e nessa faixa o overhead fixo por lote do laço
    Python que sobra (uma chamada a `quixote_native.search_nonces`, o
    cálculo do throttle e o `sleep_func` por lote) deixa de ser
    desprezível: com `BATCH_SIZE=2000`, a fração real de CPU ficava em
    torno de 3× a proporcionalidade ingênua (~16% em vez de ~5%), estável
    entre janelas de medição diferentes — não é ruído, é o custo fixo do
    laço Python por lote passando a dominar quando o alvo é uma fatia
    pequena da capacidade real.

    Medido depois (script descartável, 3 repetições de 6s por valor,
    `time.process_time()` início/fim, mesmo método deste teste):
    `BATCH_SIZE=2000` → ~15,6% de CPU; `8000` → ~15,2%; `20000` → ~14,3%;
    `50000` → ~11,5% (queda de ~26% de CPU pro mesmo 350 KH/s, sem mudar
    hashrate). Adotado `BATCH_SIZE=50000` (`.env`/`.example.env`,
    2026-09-02): reduz o overhead fixo acumulado por segundo sem estourar
    a latência de reação do `should_continue()` (pior caso teórico
    `batch_size / target_hashrate` ≈ 143 ms, aceitável pra um minerador
    solo didático sem exigência de troca de job em tempo real — a tela
    RAPL de watts nesta faixa de potência, ~0,1-0,4 W, é ruidosa demais
    pra servir de sinal direto, então a comparação foi feita em fração de
    CPU, sinal limpo e diretamente causado pela mudança). Por isso este
    teste não verifica mais proporcionalidade estrita: só que o throttle
    não está quebrado (CPU perto de rodar sem freio) nem catatônico (CPU
    perto de zero, muito abaixo até do mínimo que o overhead fixo já
    garante).
    """
    alvo = 350_000.0

    capacidade_efetiva, _ = _mine_por_segundos(target_hashrate=None, segundos=1.5)
    fracao_esperada = alvo / capacidade_efetiva

    cpu_antes = time.process_time()
    _, tempo_real = _mine_por_segundos(alvo, 2.0)
    cpu_depois = time.process_time()
    fracao_medida = (cpu_depois - cpu_antes) / tempo_real

    assert fracao_medida > fracao_esperada * 0.3  # não catatônico
    assert fracao_medida < 0.5  # bem abaixo de rodar sem freio (throttle não quebrou)
