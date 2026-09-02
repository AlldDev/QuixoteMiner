"""Telemetria de energia: três estratégias, com degradação graciosa.

Estratégia A (RAPL direto) e B (proporcional, com linha de base de ociosidade)
dependem de `energy_uj` estar legível — nesta máquina de desenvolvimento não
está, sem a regra `contrib/99-rapl-read.rules` instalada (permissão só de
root, mitigação da CVE-2020-8694). A Estratégia C (modelo) nunca falha.
"""

import logging
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_RAPL_ENERGY_PATH = pathlib.Path("/sys/class/powercap/intel-rapl:0/energy_uj")
DEFAULT_RAPL_MAX_RANGE_PATH = pathlib.Path("/sys/class/powercap/intel-rapl:0/max_energy_range_uj")

DEFAULT_TDP_WATTS = 65.0
"""TDP oficial do Ryzen 5 1600 AF, não chutado — conferido por busca em
setembro de 2026: WikiChip e TechReviewer concordam em 65 W."""

STRATEGY_RAPL = "RAPL"
STRATEGY_PROPORTIONAL = "PROPORCIONAL"
STRATEGY_MODEL = "ESTIMADO"


def read_energy_uj(path: pathlib.Path = DEFAULT_RAPL_ENERGY_PATH) -> int | None:
    """Lê o contador de energia do RAPL, em microjoules.

    Returns:
        O valor lido, ou `None` se o arquivo não existir ou não puder ser
        lido (permissão, mais comumente) — nunca lança.
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_max_energy_range_uj(path: pathlib.Path = DEFAULT_RAPL_MAX_RANGE_PATH) -> int | None:
    """Lê o valor de estouro do contador desta CPU específica.

    Cada CPU tem um `max_energy_range_uj` diferente — lido do sysfs, nunca
    chutado.
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def watts_from_energy_delta(
    energy_before_uj: int,
    t_before: float,
    energy_after_uj: int,
    t_after: float,
    max_range_uj: int,
) -> float:
    """Deriva watts a partir de duas leituras do contador de energia.

    `watts = (energia_atual - energia_anterior) / (tempo_atual -
    tempo_anterior) / 1_000_000`. Se o contador estourou entre as duas
    leituras (`energy_after_uj < energy_before_uj`), soma a volta completa.

    Args:
        energy_before_uj: leitura anterior, microjoules.
        t_before: timestamp da leitura anterior (`time.monotonic()`).
        energy_after_uj: leitura atual, microjoules.
        t_after: timestamp da leitura atual.
        max_range_uj: valor de estouro do contador (`read_max_energy_range_uj`).

    Returns:
        Potência média no intervalo, em watts.
    """
    if energy_after_uj >= energy_before_uj:
        delta_uj = energy_after_uj - energy_before_uj
    else:
        delta_uj = (max_range_uj - energy_before_uj) + energy_after_uj
    delta_t = t_after - t_before
    return delta_uj / delta_t / 1_000_000


def model_watts(cpu_fraction: float, tdp_watts: float, efficiency_factor: float = 1.0) -> float:
    """Estratégia C: estima watts a partir do TDP declarado e do uso de CPU.

    `watts = tdp_configurado * fracao_cpu_usada * fator_eficiencia`.

    Args:
        cpu_fraction: fração de um núcleo em uso, 0.0 a 1.0+.
        tdp_watts: TDP declarado da CPU.
        efficiency_factor: knob de calibração — o modelo nunca bate exato
            com a realidade, isso existe pra ajustar sem reescrever a
            fórmula. `1.0` (neutro) até haver dado real pra calibrar contra.
    """
    return tdp_watts * cpu_fraction * efficiency_factor


@dataclass(frozen=True)
class PowerReading:
    """Uma amostra de potência, com a estratégia que a produziu."""

    watts: float
    strategy: str


class PowerMeter:
    """Mede potência com a melhor estratégia disponível, degradando graciosamente.

    Uso: `calibrate_idle()` uma vez, antes do hasher começar a rodar, depois
    `sample(cpu_fraction)` periodicamente (o loop de monitor do `daemon.py`
    já roda a 1 Hz).
    """

    def __init__(
        self,
        rapl_energy_path: pathlib.Path = DEFAULT_RAPL_ENERGY_PATH,
        rapl_max_range_path: pathlib.Path = DEFAULT_RAPL_MAX_RANGE_PATH,
        tdp_watts: float = DEFAULT_TDP_WATTS,
        efficiency_factor: float = 1.0,
    ) -> None:
        self._rapl_energy_path = rapl_energy_path
        self._rapl_max_range_path = rapl_max_range_path
        self._tdp_watts = tdp_watts
        self._efficiency_factor = efficiency_factor

        self._max_range_uj: int | None = None
        self._idle_watts: float | None = None
        self._last_energy_uj: int | None = None
        self._last_energy_time: float | None = None
        self._last_kwh_time: float | None = None
        """Separado de `_last_energy_time`: esse é só pra integrar kWh em
        `sample()`, independente de o RAPL estar disponível ou não."""

        self.kwh_total = 0.0
        self._watts_sum = 0.0
        self._sample_count = 0

    def calibrate_idle(
        self, duration_seconds: float = 30.0, sleep_func: Callable[[float], None] = time.sleep
    ) -> None:
        """Mede a linha de base ociosa pra Estratégia B (proporcional).

        Só demora de verdade se o RAPL responder — se `energy_uj` não for
        legível, desiste na primeira leitura e cai direto pra Estratégia C,
        sem atrasar a inicialização do daemon à toa.

        Args:
            duration_seconds: quanto tempo medir.
            sleep_func: injetável para teste, mesmo padrão de `StratumClient`
                e `core.hasher.mine_job`.
        """
        energy_start = read_energy_uj(self._rapl_energy_path)
        if energy_start is None:
            logger.info(
                "RAPL indisponível, sem calibração de ociosidade — usando estratégia %s",
                STRATEGY_MODEL,
            )
            return

        self._max_range_uj = read_max_energy_range_uj(self._rapl_max_range_path)
        if self._max_range_uj is None:
            logger.warning(
                "RAPL respondeu mas max_energy_range_uj não — sem calibração de ociosidade"
            )
            return

        logger.info("calibrando linha de base ociosa do RAPL (%.0fs)...", duration_seconds)
        t_start = time.monotonic()
        sleep_func(duration_seconds)
        t_end = time.monotonic()
        energy_end = read_energy_uj(self._rapl_energy_path)
        if energy_end is None:
            logger.warning(
                "RAPL parou de responder durante a calibração — sem estratégia proporcional"
            )
            return

        self._idle_watts = watts_from_energy_delta(
            energy_start, t_start, energy_end, t_end, self._max_range_uj
        )
        self._last_energy_uj = energy_end
        self._last_energy_time = t_end
        logger.info(
            "linha de base ociosa: %.2f W — estratégia %s disponível",
            self._idle_watts,
            STRATEGY_PROPORTIONAL,
        )

    def sample(self, cpu_fraction: float) -> PowerReading:
        """Produz uma amostra de potência com a melhor estratégia disponível.

        Args:
            cpu_fraction: fração de um núcleo em uso (o `daemon` calcula via
                `time.process_time()`/`time.monotonic()`), usada nas
                estratégias B e C.
        """
        now = time.monotonic()
        reading = self._sample_rapl_based(cpu_fraction, now)
        if reading is None:
            reading = PowerReading(
                watts=model_watts(cpu_fraction, self._tdp_watts, self._efficiency_factor),
                strategy=STRATEGY_MODEL,
            )

        elapsed = now - self._last_kwh_time if self._last_kwh_time is not None else 0.0
        if elapsed > 0:
            self.kwh_total += reading.watts * elapsed / 3_600_000
        self._last_kwh_time = now
        self._watts_sum += reading.watts
        self._sample_count += 1
        return reading

    def _sample_rapl_based(self, cpu_fraction: float, now: float) -> PowerReading | None:
        if self._max_range_uj is None:
            return None  # RAPL nunca respondeu (nem na calibração)

        energy_now = read_energy_uj(self._rapl_energy_path)
        if energy_now is None:
            return None

        if self._last_energy_uj is None or self._last_energy_time is None:
            self._last_energy_uj = energy_now
            self._last_energy_time = now
            return None  # primeira leitura, ainda não dá pra derivar

        package_watts = watts_from_energy_delta(
            self._last_energy_uj, self._last_energy_time, energy_now, now, self._max_range_uj
        )
        self._last_energy_uj = energy_now
        self._last_energy_time = now

        if self._idle_watts is None:
            return PowerReading(watts=package_watts, strategy=STRATEGY_RAPL)

        watts_acima_do_idle = max(0.0, package_watts - self._idle_watts)
        return PowerReading(
            watts=watts_acima_do_idle * cpu_fraction, strategy=STRATEGY_PROPORTIONAL
        )

    @property
    def watts_avg(self) -> float:
        return self._watts_sum / self._sample_count if self._sample_count else 0.0
