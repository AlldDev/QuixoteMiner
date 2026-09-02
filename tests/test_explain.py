"""Cobertura de ui/explain.py (invariante 'nada sem teste')."""

from quixote.core.job import Job
from quixote.core.merkle import build_coinbase, coinbase_txid, compute_merkle_root
from quixote.ui.explain import explicar_job, montar_explicacao_job

EXTRANONCE1 = "aa11bb22"
EXTRANONCE2_SIZE = 4


def _job() -> Job:
    return Job(
        job_id="job-teste",
        prev_hash=b"\x11" * 32,
        coinb1="01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff08",
        coinb2="ffffffff0100f2052a01000000434104ac00000000",
        merkle_branch=[b"\x22" * 32, b"\x33" * 32],
        version=1,
        nbits=0x1D00FFFF,
        ntime=1305998791,
        clean_jobs=False,
    )


def test_explicar_job_nao_lanca_e_merkle_root_bate_com_a_funcao_real(capsys):
    job = _job()

    explicar_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        pool_difficulty=1.0,
        target_hashrate=350_000.0,
        batch_size=2000,
        calibrated_max_hashrate=1_000_000.0,
    )

    saida = capsys.readouterr().out

    # o laço de exibição do merkle duplica o fold de compute_merkle_root só
    # pra imprimir cada passo — aqui provamos que os dois caminhos batem.
    from quixote.core.merkle import next_extranonce2

    extranonce2 = next_extranonce2(0, EXTRANONCE2_SIZE)
    coinbase = build_coinbase(job.coinb1, EXTRANONCE1, extranonce2.hex(), job.coinb2)
    merkle_root_real = compute_merkle_root(coinbase_txid(coinbase), job.merkle_branch)

    assert merkle_root_real[::-1].hex() in saida
    assert "EXPLICAÇÃO DO JOB job-teste" in saida
    assert "TARGET EXPANDIDO" in saida
    assert "CÁLCULO DO THROTTLE" in saida
    assert "350,000" in saida  # explain.py imprime número plano, não o pt-BR do painel


def test_explicar_job_sem_capacidade_calibrada_nao_lanca(capsys):
    """Máquina recém-instalada, ainda sem calibrated_max_hashrate — não deve quebrar."""
    job = _job()

    explicar_job(
        job,
        EXTRANONCE1,
        EXTRANONCE2_SIZE,
        pool_difficulty=1.0,
        target_hashrate=350_000.0,
        batch_size=2000,
        calibrated_max_hashrate=None,
    )

    saida = capsys.readouterr().out
    assert "capacidade calibrada" not in saida


def test_montar_explicacao_job_devolve_o_mesmo_texto_que_explicar_job_imprime(capsys):
    """`explicar_job` é um wrapper fino sobre `montar_explicacao_job` — os dois batem."""
    job = _job()
    kwargs = {
        "extranonce1": EXTRANONCE1,
        "extranonce2_size": EXTRANONCE2_SIZE,
        "pool_difficulty": 1.0,
        "target_hashrate": 350_000.0,
        "batch_size": 2000,
        "calibrated_max_hashrate": 1_000_000.0,
    }

    texto = montar_explicacao_job(job, **kwargs)
    explicar_job(job, **kwargs)
    saida = capsys.readouterr().out

    assert saida == texto + "\n"
    assert "EXPLICAÇÃO DO JOB job-teste" in texto
    assert "TARGET EXPANDIDO" in texto
    assert "CÁLCULO DO THROTTLE" in texto
