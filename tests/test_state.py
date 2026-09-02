"""Cobertura de telemetry/state.py (invariante 'nada sem teste')."""

import json
import threading

from quixote.telemetry.state import HISTORY_MAXLEN, HISTORY_SAMPLE_INTERVAL_SECONDS, SharedState


def test_to_dict_traz_todos_os_campos(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    snapshot = state.to_dict()

    campos_esperados = {
        "hashrate_instant",
        "hashrate_history",
        "hashrate_avg",
        "hashes_total",
        "uptime_seconds",
        "current_job_id",
        "current_block_height",
        "current_ntime",
        "current_extranonce2",
        "pool_difficulty",
        "network_difficulty",
        "shares_accepted",
        "shares_rejected",
        "shares_accepted_total",
        "shares_rejected_total",
        "last_rejection_reason",
        "best_difficulty_session",
        "best_difficulty_ever",
        "best_difficulty_ever_timestamp",
        "blocks_found",
        "connection_state",
        "last_share_timestamp",
        "cpu_usage_percent",
        "cpu_usage_percent_maquina",
        "calibrated_max_hashrate",
        "watts_instant",
        "watts_avg",
        "power_strategy",
        "kwh_session",
        "kwh_total",
        "cost_session_brl",
        "cost_total_brl",
        "target_hashrate",
    }
    assert campos_esperados <= snapshot.keys()
    json.dumps(snapshot)  # precisa ser serializável, é isso que o IPC manda


def test_update_cpu_usage_divide_pelo_numero_de_nucleos(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state._cpu_count = 4  # evita depender do hardware que roda o teste
    state.update_cpu_usage(80.0)
    snapshot = state.to_dict()

    assert snapshot["cpu_usage_percent"] == 80.0
    assert snapshot["cpu_usage_percent_maquina"] == 20.0


def test_update_hashrate_acumula_hashes_total(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.update_hashrate(hashrate_instant=1000.0, hashes_no_lote=2000)
    state.update_hashrate(hashrate_instant=1500.0, hashes_no_lote=2000)

    snapshot = state.to_dict()
    assert snapshot["hashes_total"] == 4000
    assert snapshot["hashrate_instant"] == 1500.0


def test_record_block_found_acumula_contador(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.record_block_found()
    state.record_block_found()

    assert state.to_dict()["blocks_found"] == 2


def test_update_hashrate_amostra_historico_por_tempo_nao_por_chamada(tmp_path, monkeypatch):
    """Primeira amostra entra na hora; dentro da janela de 5s não entra de novo; depois entra."""
    relogio = {"agora": 100.0}
    monkeypatch.setattr("quixote.telemetry.state.time.monotonic", lambda: relogio["agora"])

    state = SharedState(persistence_path=tmp_path / "state.json")
    state.update_hashrate(hashrate_instant=1000.0, hashes_no_lote=1)
    assert state.to_dict()["hashrate_history"] == [1000.0]

    relogio["agora"] += 1.0  # ainda dentro da janela de 5s
    state.update_hashrate(hashrate_instant=2000.0, hashes_no_lote=1)
    assert state.to_dict()["hashrate_history"] == [1000.0]

    relogio["agora"] += HISTORY_SAMPLE_INTERVAL_SECONDS
    state.update_hashrate(hashrate_instant=3000.0, hashes_no_lote=1)
    assert state.to_dict()["hashrate_history"] == [1000.0, 3000.0]


def test_hashrate_history_descarta_amostra_mais_antiga_ao_passar_do_limite(tmp_path, monkeypatch):
    relogio = {"agora": 0.0}
    monkeypatch.setattr("quixote.telemetry.state.time.monotonic", lambda: relogio["agora"])

    state = SharedState(persistence_path=tmp_path / "state.json")
    for i in range(HISTORY_MAXLEN + 5):
        state.update_hashrate(hashrate_instant=float(i), hashes_no_lote=1)
        relogio["agora"] += HISTORY_SAMPLE_INTERVAL_SECONDS

    historico = state.to_dict()["hashrate_history"]
    assert len(historico) == HISTORY_MAXLEN
    assert historico[0] == 5.0  # as 5 primeiras amostras (0..4) caíram fora do deque
    assert historico[-1] == float(HISTORY_MAXLEN + 4)


def test_shares_aceitas_e_rejeitadas(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.record_share_accepted()
    state.record_share_rejected("Invalid ntime")
    state.record_share_accepted()

    snapshot = state.to_dict()
    assert snapshot["shares_accepted"] == 2
    assert snapshot["shares_rejected"] == 1
    assert snapshot["shares_accepted_total"] == 2
    assert snapshot["shares_rejected_total"] == 1
    assert snapshot["last_rejection_reason"] == "Invalid ntime"
    assert snapshot["last_share_timestamp"] is not None


def test_shares_total_persiste_e_soma_entre_sessoes(tmp_path):
    path = tmp_path / "state.json"

    state1 = SharedState(persistence_path=path)
    state1.record_share_accepted()
    state1.record_share_rejected("Invalid ntime")

    state2 = SharedState(persistence_path=path)
    snapshot = state2.to_dict()
    assert snapshot["shares_accepted_total"] == 1
    assert snapshot["shares_rejected_total"] == 1
    assert snapshot["shares_accepted"] == 0  # sessão nova, zerada
    assert snapshot["shares_rejected"] == 0

    state2.record_share_accepted()
    assert state2.to_dict()["shares_accepted_total"] == 2  # soma com o que já vinha persistido


def test_best_difficulty_ever_persiste_em_disco(tmp_path):
    path = tmp_path / "state.json"

    state1 = SharedState(persistence_path=path)
    state1.record_share_found(difficulty=2.5)
    assert path.exists()

    state2 = SharedState(persistence_path=path)
    assert state2.to_dict()["best_difficulty_ever"] == 2.5

    # um valor menor não sobrescreve o recorde
    state2.record_share_found(difficulty=1.0)
    assert state2.to_dict()["best_difficulty_ever"] == 2.5
    assert state2.to_dict()["best_difficulty_session"] == 1.0  # sessão é independente


def test_calibrated_max_hashrate_persiste_em_disco(tmp_path):
    path = tmp_path / "state.json"

    state1 = SharedState(persistence_path=path)
    state1.set_calibrated_max_hashrate(21_000_000.0)

    state2 = SharedState(persistence_path=path)
    assert state2.calibrated_max_hashrate == 21_000_000.0


def test_update_power_acumula_kwh_e_calcula_custo_so_com_tarifa(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")

    state.update_power(watts=30.0, watts_avg=28.0, strategy="ESTIMADO", kwh_delta=0.001)
    snapshot = state.to_dict()
    assert snapshot["watts_instant"] == 30.0
    assert snapshot["watts_avg"] == 28.0
    assert snapshot["power_strategy"] == "ESTIMADO"
    assert snapshot["kwh_session"] == 0.001
    assert snapshot["kwh_total"] == 0.001
    assert snapshot["cost_session_brl"] is None  # sem tarifa configurada
    assert snapshot["cost_total_brl"] is None

    state.set_tariff_brl_per_kwh(1.0)
    state.update_power(watts=30.0, watts_avg=28.0, strategy="ESTIMADO", kwh_delta=0.001)
    snapshot = state.to_dict()
    assert snapshot["kwh_session"] == 0.002
    assert snapshot["cost_session_brl"] == 0.002
    assert snapshot["cost_total_brl"] == 0.002


def test_kwh_total_persiste_e_soma_entre_sessoes(tmp_path):
    path = tmp_path / "state.json"

    state1 = SharedState(persistence_path=path)
    state1.update_power(watts=30.0, watts_avg=30.0, strategy="ESTIMADO", kwh_delta=0.5)

    state2 = SharedState(persistence_path=path)
    assert state2.to_dict()["kwh_total"] == 0.5
    assert state2.to_dict()["kwh_session"] == 0.0  # sessão nova, zerada

    state2.update_power(watts=30.0, watts_avg=30.0, strategy="ESTIMADO", kwh_delta=0.5)
    assert state2.to_dict()["kwh_total"] == 1.0  # soma com o que já vinha persistido


def test_update_job_propaga_altura_ntime_e_target_hashrate(tmp_path):
    """Cobertura extra: campos de `mining.notify` que só o painel consome."""
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.set_target_hashrate(350_000.0)
    state.update_job(
        "job1",
        pool_difficulty=1.0,
        network_difficulty=100_000.0,
        block_height=965063,
        ntime=1788394021,
    )
    state.update_extranonce2(b"\x00\x00\x00\x1a")

    snapshot = state.to_dict()
    assert snapshot["current_block_height"] == 965063
    assert snapshot["current_ntime"] == 1788394021
    assert snapshot["current_extranonce2"] == "0000001a"
    assert snapshot["target_hashrate"] == 350_000.0

    # job sem altura conhecida (parse_coinbase_height falhou) não quebra nada
    state.update_job("job2", pool_difficulty=1.0, network_difficulty=100_000.0)
    assert state.to_dict()["current_block_height"] is None
    assert state.to_dict()["current_ntime"] is None


def test_update_job_propaga_explicacao_pro_snapshot(tmp_path):
    """A explicação calculada pelo daemon (`e` no painel) viaja no snapshot."""
    state = SharedState(persistence_path=tmp_path / "state.json")
    assert state.to_dict()["current_job_explanation"] is None

    state.update_job(
        "job1",
        pool_difficulty=1.0,
        network_difficulty=100_000.0,
        explanation="EXPLICAÇÃO DO JOB job1",
    )
    assert state.to_dict()["current_job_explanation"] == "EXPLICAÇÃO DO JOB job1"

    # job novo sem explicação (chamada que não a calculou) não deixa lixo do job anterior
    state.update_job("job2", pool_difficulty=1.0, network_difficulty=100_000.0)
    assert state.to_dict()["current_job_explanation"] is None


def test_arquivo_persistido_corrompido_nao_derruba(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("isso não é json{{{")

    state = SharedState(persistence_path=path)
    assert state.to_dict()["best_difficulty_ever"] == 0.0


def test_mutacao_concorrente_nao_perde_contagem(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")

    def bater_share():
        for _ in range(200):
            state.record_share_accepted()

    threads = [threading.Thread(target=bater_share) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.to_dict()["shares_accepted"] == 800
