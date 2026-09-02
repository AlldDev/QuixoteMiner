"""Testes 19 a 21: telemetria de energia."""

import time

from quixote.telemetry.power import (
    STRATEGY_MODEL,
    STRATEGY_PROPORTIONAL,
    PowerMeter,
    model_watts,
    watts_from_energy_delta,
)

MAX_RANGE_UJ = 262_143_328_850  # valor real medido nesta máquina (max_energy_range_uj)


def test_watts_dentro_da_faixa_plausivel():
    """Teste 19: duas leituras plausíveis caem entre 0,5 e 200 W."""
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
    """Teste 20."""
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


def test_sem_rapl_cai_para_estrategia_estimada_sem_lancar(tmp_path):
    """Teste 21."""
    meter = PowerMeter(
        rapl_energy_path=tmp_path / "nao-existe" / "energy_uj",
        rapl_max_range_path=tmp_path / "nao-existe" / "max_energy_range_uj",
        tdp_watts=65.0,
    )

    meter.calibrate_idle(duration_seconds=999.0)  # não deve dormir nem lançar

    reading = meter.sample(cpu_fraction=0.5)
    assert reading.strategy == STRATEGY_MODEL
    assert reading.watts == model_watts(0.5, 65.0, 1.0)


def test_model_watts_bate_a_conta_na_mao():
    assert model_watts(cpu_fraction=0.5, tdp_watts=65.0, efficiency_factor=1.0) == 32.5
    assert model_watts(cpu_fraction=1.0, tdp_watts=65.0, efficiency_factor=0.5) == 32.5


def _escreve_rapl_fake(path, energy_uj: int, max_range_uj: int = MAX_RANGE_UJ) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "energy_uj").write_text(str(energy_uj))
    (path / "max_energy_range_uj").write_text(str(max_range_uj))


def test_transicao_de_rapl_para_proporcional_apos_calibrar(tmp_path):
    rapl_dir = tmp_path / "intel-rapl:0"
    _escreve_rapl_fake(rapl_dir, energy_uj=1_000_000_000)

    meter = PowerMeter(
        rapl_energy_path=rapl_dir / "energy_uj",
        rapl_max_range_path=rapl_dir / "max_energy_range_uj",
        tdp_watts=65.0,
    )

    def _sleep_fake_e_avanca_energia(_segundos: float) -> None:
        (rapl_dir / "energy_uj").write_text(str(1_000_000_000 + 6_000_000))  # +3W em ~2s simulados

    meter.calibrate_idle(duration_seconds=2.0, sleep_func=_sleep_fake_e_avanca_energia)

    # calibração terminou com idle_watts definido — qualquer amostra depois
    # disso, com o RAPL ainda respondendo, usa a estratégia proporcional.
    (rapl_dir / "energy_uj").write_text(str(1_000_000_000 + 6_000_000 + 2_000_000))
    reading = meter.sample(cpu_fraction=1.0)
    assert reading.strategy == STRATEGY_PROPORTIONAL


def test_rapl_disponivel_mas_sem_calibrar_ainda_usa_modelo(tmp_path):
    """Sem `calibrate_idle()`, `max_range_uj` nunca foi lido — mesmo com o
    RAPL respondendo, `sample()` não tem como saber disso e cai pro modelo."""
    rapl_dir = tmp_path / "intel-rapl:0"
    _escreve_rapl_fake(rapl_dir, energy_uj=1_000_000_000)

    meter = PowerMeter(
        rapl_energy_path=rapl_dir / "energy_uj",
        rapl_max_range_path=rapl_dir / "max_energy_range_uj",
        tdp_watts=65.0,
    )
    reading = meter.sample(cpu_fraction=0.5)
    assert reading.strategy == STRATEGY_MODEL


def test_kwh_total_acumula_ao_longo_de_varias_amostras(tmp_path):
    meter = PowerMeter(
        rapl_energy_path=tmp_path / "nao-existe" / "energy_uj",
        rapl_max_range_path=tmp_path / "nao-existe" / "max_energy_range_uj",
        tdp_watts=65.0,
    )
    meter.calibrate_idle()

    meter.sample(cpu_fraction=1.0)
    assert meter.kwh_total == 0.0  # primeira amostra não tem intervalo anterior

    time.sleep(0.05)
    meter.sample(cpu_fraction=1.0)
    assert meter.kwh_total > 0.0
