"""Estado compartilhado entre as threads do daemon, protegido por lock.

Carrega tudo que o throttle, o cliente Stratum e a telemetria de energia
alimentam de verdade — hashrate (instantâneo e um histórico curto pro
sparkline do painel), job atual (incluindo altura do bloco, ntime e
extranonce2), shares, watts/kWh/custo e uso de CPU — pra alimentar
o painel (`quixote top`) sem que ele precise conhecer nenhuma dessas
threads diretamente.
"""

import json
import logging
import os
import pathlib
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = pathlib.Path.home() / ".local" / "share" / "quixote" / "state.json"
"""Onde `best_difficulty_ever` e `calibrated_max_hashrate` persistem entre execuções."""

HISTORY_MAXLEN = 20
HISTORY_SAMPLE_INTERVAL_SECONDS = 5.0
"""Janela do sparkline de hashrate do painel: 20 amostras a cada 5s, ~1min40 de
histórico. `update_hashrate` é chamado por lote de hash (muito mais frequente
que isso), então a amostragem é decidida por tempo decorrido, não por chamada
— senão o deque só cobriria uma fração de segundo de histórico."""


@dataclass
class _Persisted:
    """Os únicos campos que sobrevivem a um restart do daemon, persistidos em disco."""

    best_difficulty_ever: float = 0.0
    best_difficulty_ever_timestamp: float | None = None
    calibrated_max_hashrate: float | None = None
    kwh_total: float = 0.0
    shares_accepted_total: int = 0
    shares_rejected_total: int = 0


class SharedState:
    """Estado ao vivo do minerador, protegido por `threading.Lock`.

    Cada thread (Stratum, Hasher, IPC) só toca no estado através dos
    métodos daqui, nunca lendo/escrevendo os atributos internos direto.
    """

    def __init__(self, persistence_path: pathlib.Path = DEFAULT_STATE_PATH) -> None:
        self._lock = threading.Lock()
        self._persistence_path = persistence_path
        self._persisted = self._load_persisted()

        self._start_time = time.monotonic()
        self._hashrate_instant = 0.0
        self._hashrate_history: deque[float] = deque(maxlen=HISTORY_MAXLEN)
        self._hashrate_history_last_sample = 0.0
        self._hashes_total = 0
        self._current_job_id: str | None = None
        self._current_block_height: int | None = None
        self._current_ntime: int | None = None
        self._current_extranonce2: str | None = None
        self._current_job_explanation: str | None = None
        self._pool_difficulty = 0.0
        self._network_difficulty = 0.0
        self._shares_accepted = 0
        self._shares_rejected = 0
        self._last_rejection_reason: str | None = None
        self._best_difficulty_session = 0.0
        self._blocks_found = 0
        self._connection_state = "desconectado"
        self._last_share_timestamp: float | None = None
        self._cpu_count = os.cpu_count() or 1
        self._cpu_usage_percent = 0.0
        self._cpu_usage_percent_maquina = 0.0
        self._watts_instant = 0.0
        self._watts_avg = 0.0
        self._power_strategy = "ESTIMADO"
        self._kwh_session = 0.0
        self._tariff_brl_per_kwh: float | None = None
        self._target_hashrate: float | None = None

    # --- persistência (só best_difficulty_ever e calibrated_max_hashrate) ---

    def _load_persisted(self) -> _Persisted:
        if not self._persistence_path.exists():
            return _Persisted()
        try:
            data = json.loads(self._persistence_path.read_text())
            return _Persisted(**data)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "estado persistido em %s corrompido, ignorando: %s", self._persistence_path, exc
            )
            return _Persisted()

    def _save_persisted(self) -> None:
        self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
        self._persistence_path.write_text(json.dumps(asdict(self._persisted)))

    # --- atualizações vindas do throttle (core.hasher) ---

    def update_hashrate(self, hashrate_instant: float, hashes_no_lote: int) -> None:
        with self._lock:
            self._hashrate_instant = hashrate_instant
            self._hashes_total += hashes_no_lote
            agora = time.monotonic()
            if agora - self._hashrate_history_last_sample >= HISTORY_SAMPLE_INTERVAL_SECONDS:
                self._hashrate_history.append(hashrate_instant)
                self._hashrate_history_last_sample = agora

    def record_share_found(self, difficulty: float) -> None:
        with self._lock:
            self._best_difficulty_session = max(self._best_difficulty_session, difficulty)
            if difficulty > self._persisted.best_difficulty_ever:
                self._persisted.best_difficulty_ever = difficulty
                self._persisted.best_difficulty_ever_timestamp = time.time()
                self._save_persisted()

    def record_block_found(self) -> None:
        with self._lock:
            self._blocks_found += 1

    def set_calibrated_max_hashrate(self, value: float) -> None:
        with self._lock:
            self._persisted.calibrated_max_hashrate = value
            self._save_persisted()

    def update_cpu_usage(self, percent: float) -> None:
        """Registra o uso de CPU do processo.

        Args:
            percent: fração de UM núcleo usada pelo processo, em
                percentual (o mesmo que a coluna %CPU do `top` por
                processo — pode passar de 100% se houver mais de uma
                thread ativa ao mesmo tempo). O uso relativo à máquina
                inteira é derivado daqui dividindo por `os.cpu_count()`.
        """
        with self._lock:
            self._cpu_usage_percent = percent
            self._cpu_usage_percent_maquina = percent / self._cpu_count

    def set_tariff_brl_per_kwh(self, value: float | None) -> None:
        """Tarifa de energia do usuário (`.env`, `TARIFF_BRL_PER_KWH`). Não
        persiste — é configuração, não estado medido."""
        with self._lock:
            self._tariff_brl_per_kwh = value

    def set_target_hashrate(self, value: float) -> None:
        """Hashrate-alvo (`.env`, `TARGET_HASHRATE`), pro painel mostrar o
        alvo ao lado do hashrate real. Não persiste, mesmo motivo de
        `set_tariff_brl_per_kwh`: é configuração, lida uma vez na
        inicialização do daemon."""
        with self._lock:
            self._target_hashrate = value

    def update_power(self, watts: float, watts_avg: float, strategy: str, kwh_delta: float) -> None:
        """Registra uma amostra de `telemetry.power.PowerMeter`.

        Args:
            watts: potência instantânea da amostra.
            watts_avg: `PowerMeter.watts_avg` (média da sessão).
            strategy: `"RAPL"`, `"PROPORCIONAL"` ou `"ESTIMADO"`.
            kwh_delta: kWh consumidos desde a última amostra — soma tanto
                na sessão quanto no total persistido. Salvo em disco a cada
                amostra (mesma regra do `best_difficulty`), não só ao sair,
                pra não perder consumo acumulado num crash.
        """
        with self._lock:
            self._watts_instant = watts
            self._watts_avg = watts_avg
            self._power_strategy = strategy
            self._kwh_session += kwh_delta
            self._persisted.kwh_total += kwh_delta
            self._save_persisted()

    # --- atualizações vindas do Stratum (net.stratum) ---

    def update_job(
        self,
        job_id: str,
        pool_difficulty: float,
        network_difficulty: float,
        block_height: int | None = None,
        ntime: int | None = None,
        explanation: str | None = None,
    ) -> None:
        with self._lock:
            self._current_job_id = job_id
            self._pool_difficulty = pool_difficulty
            self._network_difficulty = network_difficulty
            self._current_block_height = block_height
            self._current_ntime = ntime
            self._current_job_explanation = explanation

    def update_extranonce2(self, extranonce2: bytes) -> None:
        """Registrado pelo `on_extranonce2_change` de `core.hasher.mine_job`."""
        with self._lock:
            self._current_extranonce2 = extranonce2.hex()

    def set_connection_state(self, state: str) -> None:
        with self._lock:
            self._connection_state = state

    def record_share_accepted(self) -> None:
        with self._lock:
            self._shares_accepted += 1
            self._persisted.shares_accepted_total += 1
            self._last_share_timestamp = time.time()
            self._save_persisted()

    def record_share_rejected(self, reason: str) -> None:
        with self._lock:
            self._shares_rejected += 1
            self._persisted.shares_rejected_total += 1
            self._last_rejection_reason = reason
            self._last_share_timestamp = time.time()
            self._save_persisted()

    # --- leitura ---

    @property
    def calibrated_max_hashrate(self) -> float | None:
        with self._lock:
            return self._persisted.calibrated_max_hashrate

    def to_dict(self) -> dict[str, Any]:
        """Snapshot serializável do estado, usado pelo servidor IPC."""
        with self._lock:
            uptime_seconds = time.monotonic() - self._start_time
            hashrate_avg = self._hashes_total / uptime_seconds if uptime_seconds > 0 else 0.0
            tariff = self._tariff_brl_per_kwh
            cost_session_brl = self._kwh_session * tariff if tariff is not None else None
            cost_total_brl = self._persisted.kwh_total * tariff if tariff is not None else None
            return {
                "hashrate_instant": self._hashrate_instant,
                "hashrate_history": list(self._hashrate_history),
                "hashrate_avg": hashrate_avg,
                "hashes_total": self._hashes_total,
                "uptime_seconds": uptime_seconds,
                "current_job_id": self._current_job_id,
                "current_block_height": self._current_block_height,
                "current_ntime": self._current_ntime,
                "current_extranonce2": self._current_extranonce2,
                "current_job_explanation": self._current_job_explanation,
                "pool_difficulty": self._pool_difficulty,
                "network_difficulty": self._network_difficulty,
                "shares_accepted": self._shares_accepted,
                "shares_rejected": self._shares_rejected,
                "shares_accepted_total": self._persisted.shares_accepted_total,
                "shares_rejected_total": self._persisted.shares_rejected_total,
                "last_rejection_reason": self._last_rejection_reason,
                "best_difficulty_session": self._best_difficulty_session,
                "best_difficulty_ever": self._persisted.best_difficulty_ever,
                "best_difficulty_ever_timestamp": self._persisted.best_difficulty_ever_timestamp,
                "blocks_found": self._blocks_found,
                "connection_state": self._connection_state,
                "last_share_timestamp": self._last_share_timestamp,
                "cpu_usage_percent": self._cpu_usage_percent,
                "cpu_usage_percent_maquina": self._cpu_usage_percent_maquina,
                "calibrated_max_hashrate": self._persisted.calibrated_max_hashrate,
                "watts_instant": self._watts_instant,
                "watts_avg": self._watts_avg,
                "power_strategy": self._power_strategy,
                "kwh_session": self._kwh_session,
                "kwh_total": self._persisted.kwh_total,
                "cost_session_brl": cost_session_brl,
                "cost_total_brl": cost_total_brl,
                "target_hashrate": self._target_hashrate,
            }
