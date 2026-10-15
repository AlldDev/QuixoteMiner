"""Recalibração periódica do daemon (`daemon._calibrar(periodica=True)`):
uma medição ruidosa não pode apagar a calibração boa anterior. RAPL e a
corrida de hash são trocados por dublês — o que se testa é a decisão de
gravar ou não, não a física (essa mora em `tests/test_power_hardware.py`).
"""

from quixote import daemon
from quixote.telemetry.state import SharedState


class _ProbeFalso:
    """`EnergyProbe` com potências fixas: `measure` devolve o ocioso,
    `start`/`stop` devolvem a potência sob carga."""

    def __init__(self, ocioso: float, carga: float) -> None:
        self.available = True
        self._ocioso = ocioso
        self._carga = carga

    def measure(self, _seconds: float) -> float:
        return self._ocioso

    def start(self) -> None:
        pass

    def stop(self) -> float:
        return self._carga


def _preparar(monkeypatch, ocioso: float, carga: float, hashrate: float) -> None:
    monkeypatch.setattr(daemon, "EnergyProbe", lambda: _ProbeFalso(ocioso, carga))
    monkeypatch.setattr(daemon, "calibrate_max_hashrate", lambda _duracao: hashrate)


def test_recalibracao_valida_grava_e_devolve_true(monkeypatch, tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.set_calibration(1_000_000.0, 5e-6)
    _preparar(monkeypatch, ocioso=20.0, carga=25.0, hashrate=3_000_000.0)

    assert daemon._calibrar(state, periodica=True) is True
    assert state.calibrated_max_hashrate == 3_000_000.0
    assert state.joules_per_hash == 5.0 / 3_000_000.0


def test_recalibracao_ruidosa_mantem_a_anterior_e_devolve_false(monkeypatch, tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.set_calibration(1_000_000.0, 5e-6)
    # carga abaixo do ocioso: outro processo terminou durante a janela
    _preparar(monkeypatch, ocioso=30.0, carga=25.0, hashrate=3_000_000.0)

    assert daemon._calibrar(state, periodica=True) is False
    assert state.calibrated_max_hashrate == 1_000_000.0
    assert state.joules_per_hash == 5e-6


def test_calibracao_de_startup_ruidosa_grava_none_como_antes(monkeypatch, tmp_path):
    """No startup não há anterior confiável a preservar: grava `None` e o
    painel cai na estratégia estimada, comportamento de sempre."""
    state = SharedState(persistence_path=tmp_path / "state.json")
    _preparar(monkeypatch, ocioso=30.0, carga=25.0, hashrate=3_000_000.0)

    assert daemon._calibrar(state) is True
    assert state.calibrated_max_hashrate == 3_000_000.0
    assert state.joules_per_hash is None
