"""Orquestra as threads do daemon: Stratum, Hasher, IPC.

A coordenação Stratum↔Hasher usa o mesmo padrão já provado em
`tests/test_integration_pool.py`: um job com número de versão,
`should_continue` fechando sobre ele. `SharedState` é o único jeito de uma
thread enxergar o que a outra está fazendo.
"""

import logging
import pathlib
import signal
import threading
import time
from types import FrameType

from quixote import envfile
from quixote.core.hasher import calibrate_max_hashrate, check_target_reachable, mine_job
from quixote.core.job import Job, Share, parse_coinbase_height
from quixote.core.target import difficulty_to_target, nbits_to_target, target_to_difficulty
from quixote.net.stratum import StratumClient
from quixote.telemetry import ipc
from quixote.telemetry.power import PowerMeter
from quixote.telemetry.state import SharedState
from quixote.ui.explain import explicar_job, montar_explicacao_job

logger = logging.getLogger(__name__)

DEFAULT_TARGET_HASHRATE = 350_000.0
"""Padrão travado em 350 KH/s — o throttle é o ponto do projeto, não uma
limitação. `.env` (TARGET_HASHRATE) permite sobrescrever sem tocar aqui.
Único fallback em código desta lista: é proteção deliberada contra um
`.env` ausente/quebrado, não uma configuração comum (ver `envfile.py`)."""
MONITOR_INTERVAL_SECONDS = 1.0
POWER_STRATEGY_CHOICES = ("auto", "model")


def _read_target_hashrate(env: dict[str, str]) -> float:
    raw = env.get("TARGET_HASHRATE")
    if not raw:
        return DEFAULT_TARGET_HASHRATE
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "TARGET_HASHRATE=%r inválido no .env, usando o padrão %.0f H/s",
            raw,
            DEFAULT_TARGET_HASHRATE,
        )
        return DEFAULT_TARGET_HASHRATE


class _JobBox:
    """Guarda o job mais recente e uma versão, pro hasher saber quando reiniciar.

    A versão só avança em `clean_jobs=True` (ou no primeiro job) — chegar
    um `mining.notify` sem `clean_jobs` não deve reiniciar a busca de
    nonce em andamento, mesmo padrão de `tests/test_integration_pool.py`.
    """

    def __init__(self) -> None:
        self.job: Job | None = None
        self.version = 0

    def set(self, job: Job) -> None:
        self.job = job
        if job.clean_jobs or self.version == 0:
            self.version += 1


def run(target_hashrate: float | None = None, explain: bool = False) -> None:
    """Ponto de entrada do daemon: conecta, minera com throttle, expõe SharedState via IPC.

    Roda até receber `SIGTERM`/`SIGINT` (é o que o systemd manda num
    `stop`/`restart`).

    Args:
        target_hashrate: sobrescreve o valor do `.env`/padrão. Usado só em
            teste; a CLI real sempre deixa `None` (lê do `.env`).
        explain: se `True`, imprime uma explicação passo a passo do
            primeiro job recebido (`--explain`) antes de continuar normal.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    env = envfile.read_env(repo_root)

    address = env.get("BTC_ADDRESS")
    if not address:
        raise SystemExit("defina BTC_ADDRESS em .env antes de rodar o daemon")

    if target_hashrate is None:
        target_hashrate = _read_target_hashrate(env)

    worker_name = envfile.require_str(env, "WORKER_NAME")
    batch_size = envfile.require_int(env, "BATCH_SIZE")
    pool_host = envfile.require_str(env, "POOL_HOST")
    pool_port = envfile.require_int(env, "POOL_PORT")
    suggest_difficulty = envfile.require_float(env, "SUGGEST_DIFFICULTY")
    reconnect_max_backoff = envfile.require_float(env, "RECONNECT_MAX_BACKOFF")
    cpu_tdp_watts = envfile.require_float(env, "CPU_TDP_WATTS")
    power_strategy = envfile.require_choice(env, "POWER_STRATEGY", POWER_STRATEGY_CHOICES)

    running = threading.Event()
    running.set()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("recebido sinal %s, desligando...", signal.Signals(signum).name)
        running.clear()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    worker = f"{address}.{worker_name}"
    state = SharedState()
    state.set_target_hashrate(target_hashrate)

    tariff_raw = env.get("TARIFF_BRL_PER_KWH")
    if tariff_raw:
        try:
            state.set_tariff_brl_per_kwh(float(tariff_raw))
        except ValueError:
            logger.warning(
                "TARIFF_BRL_PER_KWH=%r inválida no .env, custo fica indisponível", tariff_raw
            )

    if state.calibrated_max_hashrate is None:
        logger.info("calibrando capacidade máxima da máquina (3s sem throttle)...")
        max_hashrate = calibrate_max_hashrate()
        state.set_calibrated_max_hashrate(max_hashrate)
        logger.info("capacidade calibrada: %.0f H/s", max_hashrate)
    check_target_reachable(target_hashrate, state.calibrated_max_hashrate or target_hashrate)

    power_meter = PowerMeter(tdp_watts=cpu_tdp_watts)
    if power_strategy == "auto":
        power_meter.calibrate_idle()  # só demora de verdade se o RAPL responder
    else:
        logger.info("POWER_STRATEGY=model: pulando calibração do RAPL, sempre Estratégia C")

    client = StratumClient(pool_host, pool_port)
    job_box = _JobBox()
    explained = False

    def _on_job(job: Job) -> None:
        nonlocal explained
        job_box.set(job)
        network_difficulty = target_to_difficulty(nbits_to_target(job.nbits))
        block_height = parse_coinbase_height(job.coinb1)
        explicacao = montar_explicacao_job(
            job,
            client.extranonce1,
            client.extranonce2_size,
            client.pool_difficulty,
            target_hashrate,
            batch_size,
            state.calibrated_max_hashrate,
        )
        state.update_job(
            job.job_id,
            client.pool_difficulty,
            network_difficulty,
            block_height,
            job.ntime,
            explicacao,
        )
        if explain and not explained:
            explained = True
            explicar_job(
                job,
                client.extranonce1,
                client.extranonce2_size,
                client.pool_difficulty,
                target_hashrate,
                batch_size,
                state.calibrated_max_hashrate,
            )

    stratum_thread = threading.Thread(
        target=client.run_forever,
        kwargs={
            "worker": worker,
            "difficulty": suggest_difficulty,
            "on_job": _on_job,
            "max_backoff_seconds": reconnect_max_backoff,
        },
        daemon=True,
        name="stratum",
    )
    stratum_thread.start()

    def _hasher_loop() -> None:
        while running.is_set():
            job = job_box.job
            if job is None:
                time.sleep(0.2)
                continue

            version = job_box.version
            target_pool = difficulty_to_target(client.pool_difficulty)

            def _submit(extranonce2: bytes, nonce: int, _job: Job = job) -> None:
                share = Share(
                    worker=worker,
                    job_id=_job.job_id,
                    extranonce2=extranonce2,
                    ntime=_job.ntime,
                    nonce=nonce,
                )
                client.submit(share)

            def _on_batch(hashes_no_lote: int, elapsed: float) -> None:
                hashrate_instant = hashes_no_lote / elapsed if elapsed > 0 else 0.0
                state.update_hashrate(hashrate_instant, hashes_no_lote)

            def _should_continue(_version: int = version) -> bool:
                return job_box.version == _version and running.is_set()

            mine_job(
                job,
                client.extranonce1,
                client.extranonce2_size,
                target_pool,
                on_share=_submit,
                should_continue=_should_continue,
                batch_size=batch_size,
                target_hashrate=target_hashrate,
                on_batch=_on_batch,
                on_share_difficulty=state.record_share_found,
                on_extranonce2_change=state.update_extranonce2,
                on_block_found=state.record_block_found,
            )

    hasher_thread = threading.Thread(target=_hasher_loop, daemon=True, name="hasher")
    hasher_thread.start()

    ipc_thread = threading.Thread(
        target=ipc.serve_forever,
        args=(state,),
        kwargs={"should_continue": running.is_set},
        daemon=True,
        name="ipc",
    )
    ipc_thread.start()

    last_cpu_time = time.process_time()
    last_wall_time = time.monotonic()
    last_accepted = 0
    last_rejected = 0
    last_power_strategy: str | None = None
    last_kwh_total = power_meter.kwh_total

    while running.is_set():
        time.sleep(MONITOR_INTERVAL_SECONDS)
        state.set_connection_state(client.connection_state)

        now_cpu = time.process_time()
        now_wall = time.monotonic()
        wall_delta = now_wall - last_wall_time
        cpu_fraction = (now_cpu - last_cpu_time) / wall_delta if wall_delta > 0 else 0.0
        if wall_delta > 0:
            state.update_cpu_usage(cpu_fraction * 100)
        last_cpu_time, last_wall_time = now_cpu, now_wall

        reading = power_meter.sample(cpu_fraction)
        kwh_delta = power_meter.kwh_total - last_kwh_total
        last_kwh_total = power_meter.kwh_total
        state.update_power(reading.watts, power_meter.watts_avg, reading.strategy, kwh_delta)
        if reading.strategy != last_power_strategy:
            logger.info("estratégia de energia: %s (%.2f W)", reading.strategy, reading.watts)
            last_power_strategy = reading.strategy

        if client.accepted_count > last_accepted:
            for _ in range(client.accepted_count - last_accepted):
                state.record_share_accepted()
            last_accepted = client.accepted_count
        if len(client.rejected_shares) > last_rejected:
            for _job_id, reason in client.rejected_shares[last_rejected:]:
                state.record_share_rejected(reason)
            last_rejected = len(client.rejected_shares)

    # a thread do IPC é daemon=True (não deve travar o processo se ficar
    # presa) mas merece a chance de rodar o `finally` e remover o socket —
    # ela só nota o `running.clear()` no próximo timeout de accept() (0.5s).
    ipc_thread.join(timeout=2.0)
    logger.info("daemon desligado")
