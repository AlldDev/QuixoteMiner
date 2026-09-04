"""Telemetria de energia: as duas estratégias e a aritmética de cada uma.

A âncora física — comparar o que este módulo diz com o que o RAPL mede de
verdade — mora em `tests/test_power_hardware.py`, marcada `slow`. Sem ela
estes testes aqui só provam que a fórmula é a fórmula: foi exatamente esse
buraco que deixou passar um erro de ~11x nos watts do painel até 2026-09-03.
"""

import time

from quixote.telemetry.power import (
    MAX_PLAUSIBLE_JOULES_PER_HASH,
    STRATEGY_MEASURED,
    STRATEGY_MODEL,
    EnergyProbe,
    PowerMeter,
    joules_per_hash,
    model_watts,
    watts_from_energy_delta,
)

MAX_RANGE_UJ = 262_143_328_850  # valor real lido de max_energy_range_uj numa máquina Intel


def test_watts_dentro_da_faixa_plausivel():
    # 3 W médios por 2 segundos = 6_000_000 microjoules de delta
    watts = watts_from_energy_delta(
        energy_before_uj=1_000_000_000,
        t_before=0.0,
        energy_after_uj=1_006_000_000,
        t_after=2.0,
        max_range_uj=MAX_RANGE_UJ,
    )
    assert 0.5 <= watts <= 200.0
    assert watts == 3.0


def test_estouro_do_contador_nao_produz_valor_absurdo():
    quase_no_topo = MAX_RANGE_UJ - 1_000_000  # falta 1_000_000 uj pra estourar
    depois_do_estouro = 5_000_000  # já deu a volta e andou mais 5_000_000 uj

    watts = watts_from_energy_delta(
        energy_before_uj=quase_no_topo,
        t_before=0.0,
        energy_after_uj=depois_do_estouro,
        t_after=2.0,
        max_range_uj=MAX_RANGE_UJ,
    )
    assert watts > 0
    assert watts < 200.0
    # delta real: 1_000_000 (até o topo) + 5_000_000 (depois de zerar) = 6_000_000 uj em 2s = 3W
    assert watts == 3.0


def test_joules_per_hash_bate_a_conta_na_mao():
    """8,4 W acima do ocioso pra 6,5 MH/s dá ~1,29 µJ por hash (ordem de
    grandeza medida de verdade nesta máquina em 2026-09-03)."""
    assert joules_per_hash(idle_watts=2.9, busy_watts=11.3, hashrate=6_500_000.0) == (
        (11.3 - 2.9) / 6_500_000.0
    )


def test_joules_per_hash_descarta_medicao_sem_sentido():
    """Máquina disputada por outro processo: o delta pode sair negativo (algo
    pesado terminou durante a janela) ou absurdo (algo pesado começou). Nos
    dois casos a calibração é descartada, não arredondada pra algo bonito."""
    assert joules_per_hash(idle_watts=11.0, busy_watts=3.0, hashrate=6_500_000.0) is None
    assert joules_per_hash(idle_watts=3.0, busy_watts=3.0, hashrate=6_500_000.0) is None
    assert joules_per_hash(idle_watts=2.9, busy_watts=11.3, hashrate=0.0) is None
    absurdo = MAX_PLAUSIBLE_JOULES_PER_HASH * 2
    assert joules_per_hash(idle_watts=0.0, busy_watts=absurdo * 10, hashrate=10.0) is None


def test_estrategia_medida_usa_hashrate_e_ignora_cpu():
    """Com J/hash calibrado, watts saem do hashrate — a fração de CPU não
    entra na conta (é justamente o que a estratégia estimada errava)."""
    meter = PowerMeter(tdp_watts=28.0, joules_per_hash=1.28e-6)

    reading = meter.sample(cpu_fraction=0.11, hashrate=350_000.0)

    assert reading.strategy == STRATEGY_MEASURED
    assert reading.watts == 350_000.0 * 1.28e-6
    assert meter.sample(cpu_fraction=0.99, hashrate=350_000.0).watts == reading.watts


def test_estrategia_medida_com_hasher_parado_da_zero():
    """Sem job ainda, hashrate é 0 e nada é atribuído à mineração."""
    meter = PowerMeter(tdp_watts=28.0, joules_per_hash=1.28e-6)
    assert meter.sample(cpu_fraction=0.05, hashrate=0.0).watts == 0.0


def test_sem_calibracao_cai_pra_estrategia_estimada():
    meter = PowerMeter(tdp_watts=65.0, joules_per_hash=None)

    reading = meter.sample(cpu_fraction=0.5, hashrate=350_000.0)

    assert reading.strategy == STRATEGY_MODEL
    assert reading.watts == model_watts(0.5, 65.0, 1.0)


def test_model_watts_bate_a_conta_na_mao():
    assert model_watts(cpu_fraction=0.5, tdp_watts=65.0, efficiency_factor=1.0) == 32.5
    assert model_watts(cpu_fraction=1.0, tdp_watts=65.0, efficiency_factor=0.5) == 32.5


def test_sem_rapl_a_sonda_nao_lanca_e_se_declara_indisponivel(tmp_path):
    """Caminho da máquina sem `energy_uj` legível (o normal fora de Linux/Intel,
    ou em qualquer distro sem a regra de udev do contrib/)."""
    probe = EnergyProbe(
        energy_path=tmp_path / "nao-existe" / "energy_uj",
        max_range_path=tmp_path / "nao-existe" / "max_energy_range_uj",
    )

    assert probe.available is False
    assert probe.measure(seconds=999.0) is None  # nem dorme nem lança
    probe.start()
    assert probe.stop() is None


def test_sonda_mede_janela_lendo_do_sysfs_falso(tmp_path):
    """Sysfs de mentira: 6 J gastos numa janela de ~0,05 s. O valor exato
    depende do relógio, então o teste checa ordem de grandeza, não igualdade."""
    energy = tmp_path / "energy_uj"
    max_range = tmp_path / "max_energy_range_uj"
    max_range.write_text(str(MAX_RANGE_UJ))
    energy.write_text("1000000000")

    probe = EnergyProbe(energy_path=energy, max_range_path=max_range)
    assert probe.available is True

    probe.start()
    time.sleep(0.05)
    energy.write_text("1006000000")  # +6 J
    watts = probe.stop()

    assert watts is not None
    assert 60.0 < watts < 200.0  # 6 J em ~0,05-0,1 s


def test_kwh_total_acumula_ao_longo_de_varias_amostras():
    meter = PowerMeter(tdp_watts=65.0)

    meter.sample(cpu_fraction=1.0)
    assert meter.kwh_total == 0.0  # primeira amostra não tem intervalo anterior

    time.sleep(0.05)
    meter.sample(cpu_fraction=1.0)
    assert meter.kwh_total > 0.0
