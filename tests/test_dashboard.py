"""Cobertura das funções puras de ui/dashboard.py (invariante 'nada sem teste').

Só o que tem lógica/ramificação: o cliente IPC e a montagem visual do
painel são I/O e renderização, verificados manualmente contra o daemon
real (mesmo espírito de `telemetry.power.PowerMeter.sample`).
"""

import io
import os
import pty
import re
import sys
import tty
from datetime import datetime

from rich.console import Console

from quixote.telemetry.state import SharedState
from quixote.ui.dashboard import (
    LINHAS_RESERVADAS_EXPLICACAO,
    _comando_pager,
    _ler_tecla_navegacao,
    _painel_explicacao,
    _quebrar_para_largura,
    _sparkline,
    capacidade_percent,
    format_numero_ptbr,
    format_tempo_relativo,
    format_timestamp_ptbr,
    format_uptime,
    formatar_destino_recompensa,
    nonce_espaco_percent,
    progresso_bloco_percent,
    render,
    scale_si,
)


def _render_para_texto(snapshot, mostrar_progresso: bool, largura: int = 100) -> str:
    console = Console(width=largura, file=io.StringIO())
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


def test_nonce_espaco_percent():
    assert nonce_espaco_percent(0) == 0.0
    assert nonce_espaco_percent(2**31) == 50.0
    assert abs(nonce_espaco_percent(2**32 - 1) - 100.0) < 1e-6
    # o ponto de operação real: a 350 KH/s, ~2 minutos de varredura dão ~1%
    assert abs(nonce_espaco_percent(350_000 * 120) - 0.98) < 0.01


def test_sparkline_escala_pelo_maximo_da_propria_lista():
    assert _sparkline([]) == ""
    assert _sparkline([0.0, 0.0]) == "▁▁"
    assert _sparkline([0.0, 50.0, 100.0]) == "▁▅█"
    assert _sparkline([10.0, 10.0]) == "██"  # tudo igual ao máximo, nível mais alto


def test_render_omite_barra_de_progresso_quando_show_progress_joke_e_falso(tmp_path):
    """Cobertura extra (Fase 8, SHOW_PROGRESS_JOKE)."""
    snapshot = SharedState(persistence_path=tmp_path / "state.json").to_dict()

    assert "até o bloco" in _render_para_texto(snapshot, mostrar_progresso=True)
    assert "até o bloco" not in _render_para_texto(snapshot, mostrar_progresso=False)


def test_render_separa_sessao_de_historico_e_mostra_o_nonce(tmp_path, bloco_candidato):
    """Os totais persistidos viajavam no snapshot e não apareciam na tela;
    o extranonce2 aparecia e era zero permanente. Este teste trava a troca."""
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.record_share_accepted()
    state.record_block_found(bloco_candidato)
    state.update_hashrate(350_000.0, hashes_no_lote=50_000, nonce_atual=2**31)
    texto = _render_para_texto(state.to_dict(), True, largura=130)

    assert "SESSÃO" in texto
    assert "HISTÓRICO" in texto
    assert "extranonce2" not in texto
    assert "nonce" in texto
    assert "80000000" in texto  # nonce em hex, sempre 8 dígitos (largura estável)
    assert "50,00%" in texto  # metade do espaço de 2**32

    # o cartão TRABALHO não pode mudar de largura conforme o nonce cresce:
    # em decimal ele iria de 5 pra 13 caracteres e reajustaria as colunas do
    # grid a cada quadro do painel (4 Hz)
    state.update_hashrate(350_000.0, hashes_no_lote=50_000, nonce_atual=85_000)
    outro = _render_para_texto(state.to_dict(), True, largura=130)
    assert "00014c08" in outro
    assert [len(linha) for linha in outro.splitlines()] == [
        len(linha) for linha in texto.splitlines()
    ]


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


def test_linha_de_destino_distingue_os_quatro_estados():
    """A conferência de destino é a única defesa contra minerar de graça pro
    pool: precisa aparecer na tela, e "ainda não sei" não pode parecer "não
    confere"."""
    aguardando = formatar_destino_recompensa(None, tem_job=False)
    assert "aguardando" in aguardando
    assert "CONFERIDO" not in aguardando

    assert "DESCONHECIDO" in formatar_destino_recompensa(None, tem_job=True)
    assert "NÃO CONFERE" in formatar_destino_recompensa(0, tem_job=True)
    assert "CONFERIDO" in formatar_destino_recompensa(317_224_234, tem_job=True)


def test_linha_de_destino_nao_mostra_valor_em_btc():
    """Regressão: a versão anterior imprimia `pagamento CONFERIDO 3,15439929
    BTC no seu endereço` e foi lida como dinheiro recebido — sem bloco
    encontrado não existe recompensa nenhuma, e o número era o subsídio +
    taxas do template daquele job, remontado a cada `mining.notify`. A linha
    fala de destino; o valor vive no log e no `--explain`."""
    for satoshis in (1, 312_500_000, 317_224_234):
        linha = formatar_destino_recompensa(satoshis, tem_job=True)
        assert "BTC" not in linha
        assert "3,1" not in linha
        assert "sats" not in linha


def test_cartao_sistema_mostra_o_destino_e_nenhum_valor(tmp_path):
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.set_coinbase_pays_us(312_500_000)
    state.update_job("job1", pool_difficulty=1.0, network_difficulty=1.0)
    snapshot = state.to_dict()

    # três larguras porque a linha é `_linha_larga` (no_wrap + ellipsis): o
    # veredito vem antes da explicação justamente pra sobreviver ao truncamento
    for largura in (100, 130, 160):
        texto = _render_para_texto(snapshot, True, largura=largura)
        assert "CONFERIDO" in texto
        assert "3,125" not in texto


LARGURAS_DE_TERMINAL = (100, 130, 160, 200)
"""As mesmas larguras em que os bugs de layout anteriores foram achados."""


def _snapshot_com_valores_largos(tmp_path):
    """Snapshot no pior caso de largura: MH/s, uptime de dias, CPU de dois dígitos."""
    state = SharedState(persistence_path=tmp_path / "state.json")
    state.update_job(
        "8447745",
        pool_difficulty=512.0,
        network_difficulty=125_807_076_547_197.5,
        block_height=965_415,
    )
    state.set_coinbase_pays_us(314_003_230)
    state.update_hashrate(6_550_000.0, hashes_no_lote=87_000, nonce_atual=3_758_591)
    snapshot = state.to_dict()
    snapshot["calibrated_max_hashrate"] = 6_550_000.0
    snapshot["target_hashrate"] = 350_000.0
    snapshot["hashrate_avg"] = 351_200.0
    snapshot["uptime_seconds"] = 6 * 86400 + 4 * 3600 + 12 * 60 + 33
    snapshot["cpu_usage_percent"] = 41.3
    snapshot["cpu_usage_percent_maquina"] = 5.2
    return snapshot


def test_hashrate_e_sistema_nao_quebram_linha_em_nenhuma_largura(tmp_path):
    """Regressão do layout reportado: HASHRATE e SISTEMA eram os dois únicos
    cartões que não saíam de `_grid_campos` — montavam os campos concatenando
    strings, então os rótulos caíam em coluna arbitrária e a terceira linha do
    HASHRATE (`média sessão … capacidade máx … (%)`) quebrava em duas em
    qualquer terminal abaixo de ~200 colunas. Dentro de um cartão de altura
    travada (`ALTURA_COLUNA_DIREITA`), a linha que quebra come a de baixo."""
    snapshot = _snapshot_com_valores_largos(tmp_path)
    rotulos = (
        "alvo",
        "média sessão",
        "capacidade máx",
        "uso",
        "recompensa",
        "progresso",
        "uptime",
        "cpu (núcleo)",
        "cpu (máquina)",
    )
    for largura in LARGURAS_DE_TERMINAL:
        texto = _render_para_texto(snapshot, True, largura=largura)
        for rotulo in rotulos:
            assert rotulo in texto, f"{rotulo!r} truncado ou quebrado em {largura} colunas"


def test_percentual_de_progresso_em_ptbr(tmp_path):
    """Todo número do painel usa vírgula decimal; o percentual de progresso era
    o único que escapava, porque saía de um `:.2e` cru."""
    texto = _render_para_texto(_snapshot_com_valores_largos(tmp_path), True, largura=130)
    assert re.search(r"\d,\d{2}e[+-]\d+% até o bloco", texto)
    assert not re.search(r"\d\.\d{2}e[+-]\d+%", texto)


def test_ler_tecla_navegacao_decodifica_setas_num_pty(monkeypatch):
    """Regressão: a seta voltava como `ESC` solto e a explicação do job saía na hora.

    `sys.stdin.read(1)` puxava os 3 bytes de `ESC [ A` do descritor pro buffer
    do `TextIOWrapper`, então o `select` seguinte via o fd vazio e a função
    desistia da sequência. Só reproduz com terminal de verdade (`termios` em
    cbreak), por isso o pty.
    """
    mestre, escravo = pty.openpty()
    tty.setcbreak(escravo)
    with open(escravo, "rb", buffering=0) as stdin_falso:
        monkeypatch.setattr(sys, "stdin", stdin_falso)
        for bytes_da_tecla, token in [
            (b"\x1b[A", "up"),
            (b"\x1b[B", "down"),
            (b"\x1b[5~", "pgup"),
            (b"\x1b[6~", "pgdn"),
            (b"q", "q"),
        ]:
            os.write(mestre, bytes_da_tecla)
            assert _ler_tecla_navegacao() == token
    os.close(mestre)


def test_explicacao_mantem_rodape_na_borda_de_baixo_em_qualquer_largura():
    """O cartão ocupa a tela inteira e o rodapé fica na borda, nunca no corpo.

    Duas regressões num teste: o rodapé dentro do corpo subia junto com o
    conteúdo, e uma linha longa (hex de coinbase) era quebrada pelo Rich
    *dentro* do cartão, deixando o Panel mais alto que a tela — o que saía
    fora era exatamente a borda de baixo.
    """
    linhas = [f"linha {i} " + ("ab12" * 40 if i == 3 else "x" * 20) for i in range(1, 41)]
    for largura, altura in [(100, 24), (130, 30), (160, 40), (60, 12)]:
        visivel = altura - LINHAS_RESERVADAS_EXPLICACAO
        roladas = _quebrar_para_largura(linhas, largura - 4)
        painel = _painel_explicacao(roladas, len(roladas) - visivel, visivel)
        console = Console(file=io.StringIO(), width=largura, height=altura, legacy_windows=False)
        console.print(painel)
        renderizado = console.file.getvalue().rstrip("\n").split("\n")

        assert len(renderizado) == altura, f"{largura}x{altura}: cartão não cabe na tela"
        assert f"/{len(roladas)}]" in renderizado[-1], "posição na rolagem fora da borda de baixo"
        assert not any("PgUp" in linha for linha in renderizado[1:-1]), "rodapé no corpo"
