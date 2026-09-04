"""Telemetria de energia: medida por RAPL quando dá, estimada por TDP quando não dá.

Duas estratégias, escolhidas sozinhas na inicialização:

`MEDIDO` — o consumo é derivado de uma constante física da máquina, os
**joules por hash**, calibrada uma vez com o contador de energia do
processador (RAPL, `/sys/class/powercap/intel-rapl:0/energy_uj`): mede-se a
potência com a máquina ociosa, depois durante uma corrida de hash sem
throttle, e a diferença dividida pelo hashrate dessa corrida dá quanto custa
um hash. Daí em diante `watts = hashrate_atual * joules_por_hash`.

O RAPL mede o pacote inteiro do processador, não este processo — a subtração
do ocioso é justamente o que isola o custo da mineração de tudo que já estava
rodando. Um processo pesado que suba *durante* a calibração contamina a
medida; por isso a calibração é descartada quando o delta sai negativo ou
absurdo, e o daemon cai na estratégia estimada.

`ESTIMADO` — sem RAPL legível (permissão de root, CPU sem driver de energia,
máquina virtual), sobra `watts = TDP_configurado * fracao_de_CPU_do_processo`.
É uma conta, não uma medição: superestimou o consumo real em ~11x nesta
máquina no ponto de operação de 350 KH/s (medido em 2026-09-03, ver o
histórico do CLAUDE.md). Serve pra ordem de grandeza e nada além disso — o
painel mostra qual das duas estratégias está em uso justamente pra essa
diferença não passar despercebida.
"""

import logging
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_RAPL_ENERGY_PATH = pathlib.Path("/sys/class/powercap/intel-rapl:0/energy_uj")
DEFAULT_RAPL_MAX_RANGE_PATH = pathlib.Path("/sys/class/powercap/intel-rapl:0/max_energy_range_uj")

STRATEGY_MEASURED = "MEDIDO"
STRATEGY_MODEL = "ESTIMADO"

MAX_PLAUSIBLE_JOULES_PER_HASH = 1e-3
"""Teto de sanidade pra calibração: 1 mJ por hash é 1 kW a 1 MH/s, absurdo
pra qualquer CPU. Delta acima disso é contaminação de outro processo pesado
durante a janela de calibração, não custo de hash."""


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


class EnergyProbe:
    """Cronômetro de energia do RAPL: abre uma janela, fecha, devolve watts.

    Existe pra separar *medir uma janela* de *decidir o que fazer com ela* —
    o daemon precisa medir uma janela ociosa e outra durante a corrida de
    calibração do hasher, que ele já roda por outro motivo.

    `available` é `False` quando o RAPL não responde (permissão de root sem
    a regra `contrib/99-rapl-read.rules`, CPU sem driver, VM); nesse caso
    `stop()` sempre devolve `None` e ninguém precisa tratar exceção.
    """

    def __init__(
        self,
        energy_path: pathlib.Path = DEFAULT_RAPL_ENERGY_PATH,
        max_range_path: pathlib.Path = DEFAULT_RAPL_MAX_RANGE_PATH,
    ) -> None:
        self._energy_path = energy_path
        self._max_range_uj = read_max_energy_range_uj(max_range_path)
        self._energy_start: int | None = None
        self._time_start = 0.0
        self.available = read_energy_uj(energy_path) is not None and self._max_range_uj is not None

    def start(self) -> None:
        """Marca o início de uma janela de medição."""
        self._energy_start = read_energy_uj(self._energy_path) if self.available else None
        self._time_start = time.monotonic()

    def stop(self) -> float | None:
        """Fecha a janela aberta por `start`.

        Returns:
            Potência média da janela em watts, ou `None` se o RAPL não
            estiver disponível, `start` não tiver sido chamado ou a leitura
            final falhar.
        """
        if self._energy_start is None or self._max_range_uj is None:
            return None
        energy_end = read_energy_uj(self._energy_path)
        if energy_end is None:
            return None
        watts = watts_from_energy_delta(
            self._energy_start, self._time_start, energy_end, time.monotonic(), self._max_range_uj
        )
        self._energy_start = None
        return watts

    def measure(
        self, seconds: float, sleep_func: Callable[[float], None] = time.sleep
    ) -> float | None:
        """Mede uma janela fechada de `seconds` (usada pra linha de base ociosa).

        Args:
            seconds: duração da janela.
            sleep_func: injetável para teste, mesmo padrão de `StratumClient`
                e `core.hasher.mine_job`.
        """
        if not self.available:
            return None
        self.start()
        sleep_func(seconds)
        return self.stop()


def joules_per_hash(idle_watts: float, busy_watts: float, hashrate: float) -> float | None:
    """Custo energético de um hash nesta máquina, em joules.

    `(potencia_sob_carga - potencia_ociosa) / hashes_por_segundo`. Subtrai o
    ocioso porque o RAPL mede o pacote inteiro: o que a mineração custa é o
    que ela *acrescenta*, não o que a máquina já gastava parada.

    Args:
        idle_watts: potência média com a máquina parada.
        busy_watts: potência média durante a corrida de hash sem throttle.
        hashrate: hashes por segundo sustentados nessa corrida.

    Returns:
        Joules por hash, ou `None` se a medição não faz sentido — delta
        negativo ou nulo (outro processo terminou durante a janela e derrubou
        a potência), hashrate não positivo, ou resultado acima de
        `MAX_PLAUSIBLE_JOULES_PER_HASH` (outro processo *subiu* durante a
        janela). Nesses casos quem chama cai na estratégia estimada em vez de
        mostrar um número inventado.
    """
    if hashrate <= 0:
        return None
    delta_watts = busy_watts - idle_watts
    if delta_watts <= 0:
        return None
    value = delta_watts / hashrate
    if value > MAX_PLAUSIBLE_JOULES_PER_HASH:
        return None
    return value


def model_watts(cpu_fraction: float, tdp_watts: float, efficiency_factor: float = 1.0) -> float:
    """Estima watts a partir do TDP declarado e do uso de CPU (estratégia `ESTIMADO`).

    `watts = tdp_configurado * fracao_cpu_usada * fator_eficiencia`.

    A conta trata a curva potência×carga como uma reta passando pela origem
    com inclinação TDP, o que ela não é: medido nesta máquina em 2026-09-03,
    o erro foi de ~3,3x com um núcleo saturado e ~11x no ponto de operação de
    350 KH/s. É fallback pra quando não há contador de energia legível, não
    uma alternativa equivalente à estratégia `MEDIDO`.

    Args:
        cpu_fraction: fração de um núcleo em uso, 0.0 a 1.0+.
        tdp_watts: TDP declarado da CPU (`CPU_TDP_WATTS` do `.env` — cada
            máquina tem o seu, o projeto não assume nenhum).
        efficiency_factor: knob de calibração — o modelo nunca bate exato
            com a realidade, isso existe pra ajustar sem reescrever a
            fórmula. `1.0` (neutro) por padrão.
    """
    return tdp_watts * cpu_fraction * efficiency_factor


@dataclass(frozen=True)
class PowerReading:
    """Uma amostra de potência, com a estratégia que a produziu."""

    watts: float
    strategy: str


class PowerMeter:
    """Converte hashrate (ou uso de CPU) em watts e acumula kWh ao longo do tempo.

    Uso: `sample(cpu_fraction, hashrate)` periodicamente (o loop de monitor
    do `daemon.py` já roda a 1 Hz).

    Args:
        tdp_watts: TDP da CPU desta máquina, `CPU_TDP_WATTS` do `.env`. Sem
            padrão de propósito: não existe TDP universal e o projeto roda em
            máquina de qualquer um.
        joules_per_hash: custo por hash calibrado (ver a função de mesmo
            nome). `None` desliga a estratégia `MEDIDO`.
        efficiency_factor: repassado pro `model_watts`.
    """

    def __init__(
        self,
        tdp_watts: float,
        joules_per_hash: float | None = None,
        efficiency_factor: float = 1.0,
    ) -> None:
        self._tdp_watts = tdp_watts
        self._joules_per_hash = joules_per_hash
        self._efficiency_factor = efficiency_factor

        self._last_kwh_time: float | None = None

        self.kwh_total = 0.0
        self._watts_sum = 0.0
        self._sample_count = 0

    def sample(self, cpu_fraction: float, hashrate: float = 0.0) -> PowerReading:
        """Produz uma amostra de potência e acumula no total de kWh.

        Args:
            cpu_fraction: fração de um núcleo em uso pelo processo (o
                `daemon` calcula via `time.process_time()`/`time.monotonic()`).
                Só usada na estratégia `ESTIMADO`.
            hashrate: hashes por segundo neste instante. Só usado na
                estratégia `MEDIDO` — com o hasher parado (sem job ainda) o
                valor é 0 e a potência atribuída à mineração é 0, que é a
                resposta certa.
        """
        now = time.monotonic()
        if self._joules_per_hash is not None:
            reading = PowerReading(
                watts=hashrate * self._joules_per_hash, strategy=STRATEGY_MEASURED
            )
        else:
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

    @property
    def watts_avg(self) -> float:
        return self._watts_sum / self._sample_count if self._sample_count else 0.0
