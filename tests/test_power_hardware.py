"""Âncora física da telemetria de energia: o que o módulo diz vs. o que o RAPL mede.

Marcado `slow` (leva ~20s e depende do hardware desta máquina), com `skip`
quando `energy_uj` não é legível — ver `contrib/README.md` pra liberar a
leitura.

É o teste que faltava. Até 2026-09-03 o painel mostrava `TDP × fração de CPU`
e errava por ~11x a 350 KH/s com toda a suíte verde, porque nenhum teste
comparava o número com a realidade: eles só conferiam que `model_watts` era
igual a `model_watts`.
"""

import pathlib
import time

import pytest
from test_throttle import _mine_por_segundos

from quixote import envfile
from quixote.core.hasher import calibrate_max_hashrate
from quixote.telemetry.power import EnergyProbe, PowerMeter, joules_per_hash

ALVO_HASHRATE = 350_000.0
"""O ponto de operação do projeto (invariante 8) — é onde a estimativa tem
que valer, não em carga total."""

FATOR_TOLERADO = 3.0
"""Quanto a previsão pode se afastar da medição. Frouxo de propósito: a
janela de medição tem ruído de outros processos e o sinal aqui é de décimos
de watt sobre uma linha de base de ~3 W. Serve pra pegar erro de ordem de
grandeza (o de 11x reprovaria), não pra certificar precisão."""

TENTATIVAS = 3
"""Repetições antes de desistir: numa máquina disputada o delta pode sair
negativo por ruído, e isso não é bug do módulo."""

pytestmark = pytest.mark.slow


def _probe_ou_skip() -> EnergyProbe:
    probe = EnergyProbe()
    if not probe.available:
        pytest.skip("RAPL não legível nesta máquina (ver contrib/99-rapl-read.rules)")
    return probe


def _tdp_ou_skip() -> float:
    """TDP desta máquina, do `.env` — o teste não inventa o número de ninguém."""
    env = envfile.read_env(pathlib.Path(__file__).resolve().parents[1])
    try:
        return envfile.require_float(env, "CPU_TDP_WATTS")
    except SystemExit:
        pytest.skip("CPU_TDP_WATTS ausente do .env")


def test_watts_previstos_batem_com_o_rapl_no_ponto_de_operacao():
    probe = _probe_ou_skip()

    ocioso = probe.measure(4.0)
    probe.start()
    capacidade = calibrate_max_hashrate(3.0)
    sob_carga = probe.stop()
    assert ocioso is not None and sob_carga is not None

    j_por_hash = joules_per_hash(ocioso, sob_carga, capacidade)
    if j_por_hash is None:
        pytest.skip(f"calibração ruidosa demais (ocioso {ocioso:.2f} W, carga {sob_carga:.2f} W)")

    meter = PowerMeter(tdp_watts=_tdp_ou_skip(), joules_per_hash=j_por_hash)
    previsto = meter.sample(cpu_fraction=0.0, hashrate=ALVO_HASHRATE).watts

    for _ in range(TENTATIVAS):
        ocioso_agora = probe.measure(4.0)
        probe.start()
        hashes, tempo = _mine_por_segundos(ALVO_HASHRATE, 8.0)
        minerando = probe.stop()
        assert ocioso_agora is not None and minerando is not None

        medido = minerando - ocioso_agora
        if medido > 0:
            hashrate_real = hashes / tempo
            assert abs(hashrate_real - ALVO_HASHRATE) / ALVO_HASHRATE < 0.05, (
                "o throttle não sustentou o alvo, a comparação de watts não valeria nada"
            )
            razao = previsto / medido
            assert 1 / FATOR_TOLERADO < razao < FATOR_TOLERADO, (
                f"previsto {previsto:.3f} W, RAPL mediu {medido:.3f} W acima do ocioso "
                f"({ocioso_agora:.2f} W) — razão {razao:.1f}x"
            )
            return
        time.sleep(1.0)

    pytest.skip("máquina ocupada demais: delta de RAPL não ficou positivo em nenhuma tentativa")


def test_modelo_tdp_erra_ordem_de_grandeza_e_por_isso_e_so_fallback():
    """Documenta com medição por que a estratégia estimada é fallback.

    Se um dia o modelo TDP passar a acertar nesta faixa, este teste falha e a
    docstring de `model_watts` (que afirma o contrário) precisa ser revista —
    é o mesmo cuidado da invariante 5, número afirmado é número medido.
    """
    probe = _probe_ou_skip()
    tdp = _tdp_ou_skip()

    ocioso = probe.measure(4.0)
    probe.start()
    cpu_antes = time.process_time()
    hashes, tempo = _mine_por_segundos(ALVO_HASHRATE, 8.0)
    fracao_cpu = (time.process_time() - cpu_antes) / tempo
    minerando = probe.stop()
    assert ocioso is not None and minerando is not None

    medido = minerando - ocioso
    if medido <= 0:
        pytest.skip("máquina ocupada demais: delta de RAPL não ficou positivo")

    estimado = PowerMeter(tdp_watts=tdp).sample(fracao_cpu, hashes / tempo).watts
    assert estimado > medido * FATOR_TOLERADO, (
        f"modelo TDP deu {estimado:.2f} W e o RAPL mediu {medido:.3f} W — se a diferença "
        "encolheu, revisar a docstring de model_watts"
    )
