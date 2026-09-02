"""Cobertura das funções puras de ui/dashboard.py (invariante 'nada sem teste').

Só o que tem lógica/ramificação: o cliente IPC e a montagem visual do
painel são I/O e renderização, verificados manualmente contra o daemon
real (mesmo espírito de `telemetry.power.PowerMeter.sample`).
"""

import io
from datetime import datetime

from rich.console import Console

from quixote.telemetry.state import SharedState
from quixote.ui.dashboard import (
    _comando_pager,
    _sparkline,
    capacidade_percent,
    format_numero_ptbr,
    format_tempo_relativo,
    format_timestamp_ptbr,
    format_uptime,
    progresso_bloco_percent,
    render,
    scale_si,
)


def _render_para_texto(snapshot, mostrar_progresso: bool) -> str:
    console = Console(width=100, file=io.StringIO())
    console.print(render(snapshot, mostrar_progresso))
    return console.file.getvalue()


def test_format_numero_ptbr_virgula_decimal_e_ponto_de_milhar():
    assert format_numero_ptbr(1234.5, 1) == "1.234,5"
    assert format_numero_ptbr(0.042, 3) == "0,042"
    assert format_numero_ptbr(2, 0) == "2"


def test_scale_si_escala_hashrate():
    assert scale_si(348_200, "H/s") == "348,2 KH/s"
    assert scale_si(21_400_000, "H/s") == "21,4 MH/s"
    assert scale_si(500, "H/s") == "500,0 H/s"
    assert scale_si(0, "H/s") == "0,0 H/s"


def test_scale_si_sem_sufixo_pra_dificuldade():
    assert scale_si(142_300_000_000_000) == "142,3 T"
    assert scale_si(1.0) == "1,0"


def test_format_uptime():
    assert format_uptime(0) == "0d 00:00:00"
    assert format_uptime(6 * 86400 + 4 * 3600 + 12 * 60 + 33) == "6d 04:12:33"


def test_format_tempo_relativo():
    agora = 1_000_000.0
    assert format_tempo_relativo(None, agora) == "nunca"
    assert format_tempo_relativo(agora - 30, agora) == "há 30s"
    assert format_tempo_relativo(agora - 300, agora) == "há 5m"
    assert format_tempo_relativo(agora - 9720, agora) == "há 2h 42m"
    assert format_tempo_relativo(agora - 100_000, agora) == "há 1d 3h"


def test_format_timestamp_ptbr():
    assert format_timestamp_ptbr(None) == "nunca"
    timestamp = datetime(2026, 8, 28, 3, 14).astimezone().timestamp()
    assert format_timestamp_ptbr(timestamp) == "28/08/2026 03:14"


def test_progresso_bloco_percent():
    assert progresso_bloco_percent(0, 100_000.0) == 0.0
    assert progresso_bloco_percent(100, 0.0) == 0.0
    # dificuldade 1: espera-se 2**32 hashes pra 100% — meio caminho em 2**31
    assert abs(progresso_bloco_percent(2**31, 1.0) - 50.0) < 1e-6


def test_capacidade_percent():
    assert capacidade_percent(175_000.0, 350_000.0) == 50.0
    assert capacidade_percent(175_000.0, None) is None
    assert capacidade_percent(175_000.0, 0.0) is None


def test_sparkline_escala_pelo_maximo_da_propria_lista():
    assert _sparkline([]) == ""
    assert _sparkline([0.0, 0.0]) == "▁▁"
    assert _sparkline([0.0, 50.0, 100.0]) == "▁▅█"
    assert _sparkline([10.0, 10.0]) == "██"  # tudo igual ao máximo, nível mais alto


def test_render_omite_barra_de_progresso_quando_show_progress_joke_e_falso(tmp_path):
    """Cobertura extra (Fase 8, SHOW_PROGRESS_JOKE)."""
    snapshot = SharedState(persistence_path=tmp_path / "state.json").to_dict()

    assert "progresso até o bloco" in _render_para_texto(snapshot, mostrar_progresso=True)
    assert "progresso até o bloco" not in _render_para_texto(snapshot, mostrar_progresso=False)


def test_comando_pager():
    assert _comando_pager("less", "/var/log/quixote/quixote.log") == [
        "less",
        "+G",
        "/var/log/quixote/quixote.log",
    ]
    assert _comando_pager("cat", "/var/log/quixote/quixote.log") == [
        "cat",
        "/var/log/quixote/quixote.log",
    ]
