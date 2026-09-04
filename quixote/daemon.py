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
from quixote.core.job import BlockCandidate, Job, Share, parse_coinbase_height
from quixote.core.merkle import build_coinbase
from quixote.core.payout import address_to_script_pubkey, coinbase_payout_to
from quixote.core.target import difficulty_to_target, nbits_to_target, target_to_difficulty
from quixote.net.stratum import StratumClient
from quixote.telemetry import ipc
from quixote.telemetry.power import EnergyProbe, PowerMeter, joules_per_hash
from quixote.telemetry.state import SharedState
from quixote.ui.explain import explicar_job, montar_explicacao_job

logger = logging.getLogger(__name__)

DEFAULT_TARGET_HASHRATE = 350_000.0
"""Padrão travado em 350 KH/s — o throttle é o ponto do projeto, não uma
limitação. `.env` (TARGET_HASHRATE) permite sobrescrever sem tocar aqui.
Único fallback em código desta lista: é proteção deliberada contra um
`.env` ausente/quebrado, não uma configuração comum (ver `envfile.py`)."""
MONITOR_INTERVAL_SECONDS = 1.0

CALIBRATION_MAX_AGE_SECONDS = 3 * 24 * 3600
"""Calibração mais velha que isso é refeita. Ela mede hardware (capacidade
de hash e joules por hash), e hardware não muda sozinho — mas o resto muda:
outro perfil de energia, outro kernel, o binário Rust recompilado, a máquina
virada de mesa pra notebook na bateria. Três dias é a escolha do usuário
(2026-09-03); o custo de errar pra menos é ~5s de inicialização."""

CALIBRATION_IDLE_SECONDS = 2.0
"""Janela ociosa medida antes da corrida de calibração, pra descontar do
consumo o que a máquina já gastava parada."""


def _calibrar(state: SharedState) -> None:
    """Mede capacidade máxima de hash e custo energético por hash, e persiste.

    As duas saem da mesma corrida sem throttle: primeiro uma janela parada
    (linha de base do RAPL), depois `calibrate_max_hashrate` com o contador
    de energia aberto em volta. Roda antes das threads de Stratum e do hasher
    subirem, então o único ruído possível é de processos de fora.
    """
    probe = EnergyProbe()
    if not probe.available:
        logger.info(
            "contador de energia do RAPL indisponível (permissão ou CPU sem suporte) — "
            "watts vão sair do modelo TDP, ver contrib/README.md"
        )

    logger.info("medindo linha de base ociosa (%.0fs)...", CALIBRATION_IDLE_SECONDS)
    idle_watts = probe.measure(CALIBRATION_IDLE_SECONDS)

    logger.info("calibrando capacidade máxima da máquina (3s sem throttle)...")
    probe.start()
    max_hashrate = calibrate_max_hashrate()
    busy_watts = probe.stop()

    j_por_hash = (
        joules_per_hash(idle_watts, busy_watts, max_hashrate)
        if idle_watts is not None and busy_watts is not None
        else None
    )
    state.set_calibration(max_hashrate, j_por_hash)

    logger.info("capacidade calibrada: %.0f H/s", max_hashrate)
    if j_por_hash is not None:
        logger.info(
            "custo de energia calibrado: %.3f µJ por hash (ocioso %.2f W, sob carga %.2f W)",
            j_por_hash * 1e6,
            idle_watts,
            busy_watts,
        )
    elif idle_watts is not None and busy_watts is not None:
        logger.warning(
            "calibração de energia descartada (ocioso %.2f W, sob carga %.2f W): a máquina não "
            "estava parada o bastante — watts vão sair do modelo TDP",
            idle_watts,
            busy_watts,
        )


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

    # o pool responde `result: true` no mining.authorize pra praticamente
    # qualquer string, então a autorização não valida nada — um typo ou um
    # endereço de testnet só apareceria no dia do bloco, quando não há mais
    # o que fazer. Decodificar aqui falha na partida, de graça.
    try:
        script_pubkey_esperado = address_to_script_pubkey(address)
    except ValueError as exc:
        raise SystemExit(f"BTC_ADDRESS em .env é inválido: {exc}") from None

    if target_hashrate is None:
        target_hashrate = _read_target_hashrate(env)

    worker_name = envfile.require_str(env, "WORKER_NAME")
    batch_size = envfile.require_int(env, "BATCH_SIZE")
    pool_host = envfile.require_str(env, "POOL_HOST")
    pool_port = envfile.require_int(env, "POOL_PORT")
    suggest_difficulty = envfile.require_float(env, "SUGGEST_DIFFICULTY")
    reconnect_max_backoff = envfile.require_float(env, "RECONNECT_MAX_BACKOFF")
    cpu_tdp_watts = envfile.require_float(env, "CPU_TDP_WATTS")

    running = threading.Event()
    running.set()

    destino_conferido = threading.Event()
    """Fica limpo até um job provar que a coinbase paga o `BTC_ADDRESS`.

    Confere destino da recompensa, não recebimento: nada é pago sem bloco.
    Começa limpo de propósito: enquanto nenhum job foi conferido, não há por
    que hashear. Se um job deixar de conferir, o hasher para e volta sozinho
    quando um job seguinte conferir — falha fechada, sem derrubar o serviço.
    """

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

    idade = state.calibration_age_seconds
    if (
        state.calibrated_max_hashrate is None
        or idade is None
        or idade > CALIBRATION_MAX_AGE_SECONDS
    ):
        _calibrar(state)
    check_target_reachable(target_hashrate, state.calibrated_max_hashrate or target_hashrate)

    power_meter = PowerMeter(tdp_watts=cpu_tdp_watts, joules_per_hash=state.joules_per_hash)

    client = StratumClient(pool_host, pool_port)
    job_box = _JobBox()
    explained = False

    def _verificar_destino_da_recompensa(job: Job) -> None:
        """Confere se a coinbase deste job paga o endereço configurado.

        Confere **destino**, não recebimento: sem bloco encontrado não existe
        recompensa, e o valor que aparece no log é o subsídio + taxas deste
        template, que o pool remonta a cada `mining.notify`.

        Roda uma vez por job (nunca por nonce): o `scriptPubKey` das saídas
        não depende do extranonce, então a coinbase é montada com um
        `extranonce2` zerado só para ter uma transação bem formada de onde
        percorrer as saídas.
        """
        coinbase = build_coinbase(
            job.coinb1,
            client.extranonce1,
            "00" * client.extranonce2_size,
            job.coinb2,
        )
        satoshis = coinbase_payout_to(coinbase, script_pubkey_esperado)
        state.set_coinbase_pays_us(satoshis)
        if satoshis is None:
            destino_conferido.clear()
            logger.critical(
                "coinbase do job %s não pôde ser percorrida — mineração parada, "
                "não dá para afirmar que o pagamento é seu",
                job.job_id,
            )
        elif satoshis == 0:
            destino_conferido.clear()
            logger.critical(
                "coinbase do job %s paga OUTRO endereço, não %s — mineração parada",
                job.job_id,
                address,
            )
        else:
            if not destino_conferido.is_set():
                logger.info(
                    "destino da recompensa conferido: a coinbase do job %s pagaria "
                    "%.8f BTC (subsídio + taxas deste template) em %s se um hash "
                    "bater o target da rede",
                    job.job_id,
                    satoshis / 1e8,
                    address,
                )
            destino_conferido.set()

    def _on_job(job: Job) -> None:
        nonlocal explained
        job_box.set(job)
        _verificar_destino_da_recompensa(job)
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
            script_pubkey_esperado,
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
                script_pubkey_esperado,
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
            if job is None or not destino_conferido.is_set():
                time.sleep(0.2)
                continue

            version = job_box.version
            epoch = client.session_epoch
            target_pool = difficulty_to_target(client.pool_difficulty)
            # lista, e não uma variável com `nonlocal`, pra poder ser passada
            # como argumento default às duas closures — é como o resto deste
            # laço evita capturar variável de iteração por referência
            blocos_deste_job: list[BlockCandidate] = []

            def _on_block_found(
                candidato: BlockCandidate, _blocos: list[BlockCandidate] = blocos_deste_job
            ) -> None:
                _blocos.append(candidato)
                state.record_block_found(candidato)

            def _submit(
                extranonce2: bytes,
                nonce: int,
                _job: Job = job,
                _blocos: list[BlockCandidate] = blocos_deste_job,
            ) -> None:
                share = Share(
                    worker=worker,
                    job_id=_job.job_id,
                    extranonce2=extranonce2,
                    ntime=_job.ntime,
                    nonce=nonce,
                )
                if client.submit(share):
                    return
                bloco = next((b for b in _blocos if b.nonce == nonce), None)
                if bloco is not None:
                    logger.critical(
                        "BLOCO NÃO ENVIADO (sem conexão no instante do envio): hash=%s, "
                        "candidato gravado em disco, remontável a partir do header",
                        bloco.block_hash_display,
                    )
                    # ponytail: sem reenvio automático, o extranonce1 da sessão
                    # nova invalida a share e o pool recusaria o job_id antigo
                else:
                    logger.warning("share do job %s perdida: sem conexão", _job.job_id)

            def _on_batch(hashes_no_lote: int, elapsed: float, start_nonce: int) -> None:
                hashrate_instant = hashes_no_lote / elapsed if elapsed > 0 else 0.0
                state.update_hashrate(hashrate_instant, hashes_no_lote, start_nonce)

            def _should_continue(_version: int = version, _epoch: int = epoch) -> bool:
                # o epoch entra junto da versão do job: uma reconexão troca o
                # extranonce1 sem necessariamente trazer um clean_jobs, e
                # continuar com o antigo produz coinbase inválida
                return (
                    job_box.version == _version
                    and client.session_epoch == _epoch
                    and destino_conferido.is_set()
                    and running.is_set()
                )

            try:
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
                    on_block_found=_on_block_found,
                )
            except Exception:
                # esta thread não é reiniciada por ninguém: qualquer exceção
                # que escapasse daqui pararia a mineração em definitivo com o
                # processo vivo e o painel desenhando normalmente
                logger.exception("erro no laço de mineração do job %s, seguindo", job.job_id)
                time.sleep(1.0)

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
    threads_mortas: set[str] = set()

    while running.is_set():
        time.sleep(MONITOR_INTERVAL_SECONDS)
        if not running.is_set():
            # o sinal pode chegar durante o sleep: sem isto, o resto do corpo
            # roda uma última vez com as threads já se encerrando e a
            # supervisão abaixo acusa "thread morreu" num desligamento normal
            break
        state.set_connection_state(client.connection_state)

        for thread in (hasher_thread, stratum_thread, ipc_thread):
            if not thread.is_alive() and thread.name not in threads_mortas:
                # nenhuma delas deveria morrer antes do shutdown; se morrer, o
                # processo continua vivo e o painel continua desenhando, então
                # o log é o único lugar onde isso pode aparecer. Uma vez só:
                # o monitor passa por aqui a cada segundo.
                threads_mortas.add(thread.name)
                logger.critical("thread %s morreu — reinicie o daemon", thread.name)

        now_cpu = time.process_time()
        now_wall = time.monotonic()
        wall_delta = now_wall - last_wall_time
        cpu_fraction = (now_cpu - last_cpu_time) / wall_delta if wall_delta > 0 else 0.0
        if wall_delta > 0:
            state.update_cpu_usage(cpu_fraction * 100)
        last_cpu_time, last_wall_time = now_cpu, now_wall

        reading = power_meter.sample(cpu_fraction, state.hashrate_instant)
        kwh_delta = power_meter.kwh_total - last_kwh_total
        last_kwh_total = power_meter.kwh_total
        state.set_watts_instant(reading.watts)
        state.update_power(power_meter.watts_avg, reading.strategy, kwh_delta)
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
