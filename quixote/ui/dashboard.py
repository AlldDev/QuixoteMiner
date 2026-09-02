"""Painel `rich` ao vivo: cliente do socket IPC do daemon.

Processo separado do daemon — só lê `SharedState.to_dict()` via
`telemetry.ipc`, nunca minera nem toca na conexão Stratum. Fechar o painel
não para a mineração.

Formatação numérica em padrão pt-BR (vírgula decimal, ponto de milhar) em
todo o painel, por decisão explícita do usuário, consistente com o resto
da interface já em português. `q` (sair) e `l` (abrir os logs) têm handler
de teclado de verdade (modo cbreak do terminal, só quando `stdin` é um
tty). `r` (reconectar) continua só texto — exigiria IPC bidirecional, hoje
o socket só empurra estado do servidor pro cliente — e `e` (explicar) é
uma flag de inicialização do daemon (`--explain`), não uma ação do painel.
"""

import contextlib
import json
import logging
import os
import select
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from quixote import envfile
from quixote.telemetry.ipc import DEFAULT_SOCKET_PATH

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 1.0
BARRA_LARGURA = 28

ESCALAS_SI = [(1e15, "P"), (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "K"), (0.0, "")]

CORES_CONEXAO = {
    "conectado": "green",
    "conectando": "yellow",
    "reconectando": "yellow",
    "desconectado": "red",
}

# Paleta do redesign "1b" (Claude Design): borda neutra em todos os cartões,
# menos o de destaque (HASHRATE); os outros se distinguem só pela cor do
# título, via markup, mesmo padrão já usado no status de conexão do título
# externo.
COR_BORDA_NEUTRA = "#3b4261"
COR_HASHRATE = "#9ece6a"
COR_ENERGIA = "#bb9af7"
COR_TRABALHO = "#7aa2f7"
COR_RESULTADOS = "#9ece6a"
COR_LABEL = "#565f89"  # rótulo de todo campo label/valor, sempre este tom — só o valor muda de cor
COR_EXTRANONCE2 = "#7dcfff"  # único valor com cor própria no mockup, mantido aqui

SPARKLINE_NIVEIS = "▁▂▃▄▅▆▇█"

ALTURA_CARTAO_PADRAO = 5  # 3 linhas de conteúdo + 2 de borda (ENERGIA/RESULTADOS/TRABALHO)
ALTURA_COLUNA_DIREITA = ALTURA_CARTAO_PADRAO * 2  # linha de cima + TRABALHO embaixo
# ponytail: número fixo, calculado a partir da estrutura atual dos 3 cartões da
# direita — se algum ganhar/perder linha de conteúdo, atualizar junto. Medir a
# altura renderizada de verdade (duas passadas) resolveria isso sozinho, mas é
# esforço maior do que o problema pede agora.


# --- funções puras de formatação/cálculo (testadas em tests/test_dashboard.py) ---


def format_numero_ptbr(valor: float, casas: int) -> str:
    """Formata `valor` no padrão pt-BR: vírgula decimal, ponto de milhar."""
    bruto = f"{valor:,.{casas}f}"
    return bruto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def scale_si(valor: float, sufixo: str = "") -> str:
    """Escala `valor` pra K/M/G/T/P e formata com uma casa decimal, pt-BR.

    `sufixo` é anexado depois da letra de escala (`"H/s"` -> `"348,2 KH/s"`;
    `""` -> só a letra, usado pra dificuldade de rede, ex. `"142,3 T"`).
    """
    for limite, letra in ESCALAS_SI:
        if valor >= limite:
            escalado = valor / limite if limite else valor
            unidade = f"{letra}{sufixo}"
            numero = format_numero_ptbr(escalado, 1)
            return f"{numero} {unidade}" if unidade else numero
    return format_numero_ptbr(valor, 1)  # inalcançável (limite=0.0 sempre casa), só pro mypy


def format_uptime(segundos: float) -> str:
    """`"6d 04:12:33"` a partir de segundos corridos."""
    segundos_int = int(segundos)
    dias, resto = divmod(segundos_int, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, segs = divmod(resto, 60)
    return f"{dias}d {horas:02d}:{minutos:02d}:{segs:02d}"


def format_tempo_relativo(timestamp: float | None, agora: float) -> str:
    """`"há 2h 41m"` a partir de um timestamp Unix, ou `"nunca"` se `None`."""
    if timestamp is None:
        return "nunca"
    delta = max(0, int(agora - timestamp))
    if delta < 60:
        return f"há {delta}s"
    if delta < 3600:
        return f"há {delta // 60}m"
    if delta < 86400:
        return f"há {delta // 3600}h {(delta % 3600) // 60}m"
    return f"há {delta // 86400}d {(delta % 86400) // 3600}h"


def format_timestamp_ptbr(timestamp: float | None) -> str:
    """`"28/08/2026 03:14"` a partir de um timestamp Unix, ou `"nunca"`."""
    if timestamp is None:
        return "nunca"
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone().strftime("%d/%m/%Y %H:%M")


def progresso_bloco_percent(hashes_total: int, network_difficulty: float) -> float:
    """% do caminho até um bloco, na expectativa estatística.

    Um bloco de dificuldade 1 espera, em média, `2**32` hashes. Escalado
    pela dificuldade de rede, dá o número esperado de hashes pra achar um
    bloco de verdade — é o denominador da barra de progresso irônica do
    painel, que mostra exatamente o quanto está perto (nada perto) de
    resolver um bloco de verdade.
    """
    if network_difficulty <= 0:
        return 0.0
    esperado = network_difficulty * 2**32
    return hashes_total / esperado * 100


def capacidade_percent(hashrate_avg: float, calibrated_max_hashrate: float | None) -> float | None:
    """% da capacidade máxima calibrada da máquina em uso, ou `None` sem calibração."""
    if not calibrated_max_hashrate:
        return None
    return hashrate_avg / calibrated_max_hashrate * 100


def _barra(fracao: float, largura: int = BARRA_LARGURA) -> str:
    fracao = min(1.0, max(0.0, fracao))
    cheios = round(fracao * largura)
    return "█" * cheios + "░" * (largura - cheios)


def _sparkline(valores: list[float]) -> str:
    """Uma linha de blocos Unicode com o histórico recente de hashrate.

    Cada valor vira o nível de bloco proporcional ao máximo da própria
    lista (não a um alvo fixo) — o objetivo é mostrar variação recente,
    não capacidade. `""` sem histórico ainda (painel recém-aberto).
    """
    if not valores:
        return ""
    maximo = max(valores) or 1.0
    niveis = len(SPARKLINE_NIVEIS)
    return "".join(SPARKLINE_NIVEIS[min(niveis - 1, int(v / maximo * niveis))] for v in valores)


# --- render (I/O visual, verificado manualmente contra o daemon real) ---


def _moeda_brl(valor: float | None) -> str:
    return "—" if valor is None else f"R$ {format_numero_ptbr(valor, 2)}"


def _grid_campos(*linhas: tuple[tuple[str, str], ...]) -> Table:
    """Tabela de campos label/valor — todas as linhas na MESMA tabela.

    Montar uma mini-`Table.grid` por linha (como antes) faz cada uma
    calcular a própria largura de coluna, e colunas de linhas diferentes
    não alinham entre si — é a causa do texto "torto" que o usuário
    reportou. Uma tabela só, com uma `add_row` por linha, resolve isso:
    largura de coluna é uma só, compartilhada, coerente em todo o cartão.
    O rótulo sempre sai em `COR_LABEL` (mesmo tom do mockup), o valor no
    estilo default — só o número em si chama atenção, não o rótulo.
    `no_wrap=True` mantém 1 linha por linha de campo mesmo em terminal
    estreito (trunca com "…" em vez de quebrar) — sem isso a altura do
    cartão varia com a largura do terminal, quebrando a suposição de
    `ALTURA_COLUNA_DIREITA`.
    """
    grid = Table.grid(padding=(0, 2))
    for _ in linhas[0]:
        grid.add_column(no_wrap=True)
        grid.add_column(no_wrap=True, overflow="ellipsis")
    for linha in linhas:
        celulas = [
            item
            for label, valor in linha
            for item in (f"[{COR_LABEL}]{label}[/{COR_LABEL}]", valor)
        ]
        grid.add_row(*celulas)
    return grid


def _secao_hashrate(s: dict[str, Any]) -> Panel:
    """Cartão de destaque do painel (mockup 1b) — inclui o sparkline de histórico."""
    alvo = s.get("target_hashrate")
    instant = scale_si(s["hashrate_instant"], "H/s")
    media = scale_si(s["hashrate_avg"], "H/s")
    capacidade = s.get("calibrated_max_hashrate")
    cap_percent = capacidade_percent(s["hashrate_avg"], capacidade)
    linha1 = f"[bold {COR_HASHRATE}]{instant}[/bold {COR_HASHRATE}]"
    if alvo:
        linha1 += f"    [{COR_LABEL}]alvo[/{COR_LABEL}] {scale_si(alvo, 'H/s')}"
    spark = _sparkline(s["hashrate_history"])
    linha_spark = spark if spark else "[dim]aguardando histórico...[/dim]"
    linha3 = f"[{COR_LABEL}]média sessão[/{COR_LABEL}] {media}"
    if capacidade:
        linha3 += (
            f"    [{COR_LABEL}]capacidade máx[/{COR_LABEL}] {scale_si(capacidade, 'H/s')}"
            f" ({format_numero_ptbr(cap_percent or 0.0, 1)}%)"
        )
    corpo = Group(linha1, linha_spark, linha3)
    return Panel(corpo, title="HASHRATE", border_style=COR_HASHRATE, height=ALTURA_COLUNA_DIREITA)


def _secao_energia(s: dict[str, Any]) -> Panel:
    custo_sessao = s.get("cost_session_brl")
    custo_total = s.get("cost_total_brl")
    grid = _grid_campos(
        (
            ("agora", format_numero_ptbr(s["watts_instant"], 2) + " W"),
            ("média", format_numero_ptbr(s["watts_avg"], 2) + " W"),
        ),
        (
            ("sessão", format_numero_ptbr(s["kwh_session"], 3) + " kWh"),
            ("total", format_numero_ptbr(s["kwh_total"], 3) + " kWh"),
        ),
        (("custo", _moeda_brl(custo_sessao)), ("total", _moeda_brl(custo_total))),
    )
    titulo = f"[{COR_ENERGIA}]ENERGIA [{s['power_strategy']}][/{COR_ENERGIA}]"
    return Panel(grid, title=titulo, border_style=COR_BORDA_NEUTRA)


def _secao_trabalho(s: dict[str, Any]) -> Panel:
    altura = s.get("current_block_height")
    extranonce2 = s.get("current_extranonce2")
    valor_extranonce2 = (
        f"[{COR_EXTRANONCE2}]{extranonce2}[/{COR_EXTRANONCE2}]" if extranonce2 else "—"
    )
    grid = _grid_campos(
        (
            ("job", s["current_job_id"] or "—"),
            ("altura", format_numero_ptbr(altura, 0) if altura is not None else "—"),
        ),
        (
            ("dif pool", scale_si(s["pool_difficulty"])),
            ("dif rede", scale_si(s["network_difficulty"])),
        ),
        (("extranonce2", valor_extranonce2), ("ntime", str(s.get("current_ntime") or "—"))),
    )
    titulo = f"[{COR_TRABALHO}]TRABALHO[/{COR_TRABALHO}]"
    return Panel(grid, title=titulo, border_style=COR_BORDA_NEUTRA)


def _secao_resultados(s: dict[str, Any]) -> Panel:
    """Cartão de resultados (mockup 1b) — "última share" mora aqui, não no rodapé.

    Sempre 3 linhas de conteúdo (motivo de rejeição embutido na célula
    "rejeitadas", não numa linha própria) — mesma altura de ENERGIA/TRABALHO,
    pro cartão HASHRATE poder casar a altura com a coluna inteira à direita
    (ver `ALTURA_COLUNA_DIREITA`).
    """
    agora = time.time()
    rejeitadas = str(s["shares_rejected"])
    if s.get("last_rejection_reason"):
        rejeitadas += f" [dim]({s['last_rejection_reason']})[/dim]"
    grid = _grid_campos(
        (("aceitas", str(s["shares_accepted"])), ("rejeitadas", rejeitadas)),
        (
            ("melhor sessão", format_numero_ptbr(s["best_difficulty_session"], 4)),
            ("melhor sempre", format_numero_ptbr(s["best_difficulty_ever"], 4)),
        ),
        (
            ("blocos", str(s["blocks_found"])),
            ("última share", format_tempo_relativo(s.get("last_share_timestamp"), agora)),
        ),
    )
    titulo = f"[{COR_RESULTADOS}]RESULTADOS[/{COR_RESULTADOS}]"
    return Panel(grid, title=titulo, border_style=COR_BORDA_NEUTRA)


def render(snapshot: dict[str, Any] | None, mostrar_progresso: bool = True) -> Panel:
    """Monta o painel inteiro a partir de um snapshot de `SharedState.to_dict()`.

    Args:
        snapshot: estado atual, ou `None` antes da primeira conexão.
        mostrar_progresso: se `False`, omite a barra "progresso até o
            bloco" (`.env`, `SHOW_PROGRESS_JOKE`).
    """
    if snapshot is None:
        return Panel("aguardando conexão com o daemon...", title="quixote", border_style="yellow")

    s = snapshot
    rodape = (
        f"uptime {format_uptime(s['uptime_seconds'])}"
        f"     cpu (uso no núcleo) {format_numero_ptbr(s['cpu_usage_percent'], 1)}%"
        f"     cpu (uso total da máquina) {format_numero_ptbr(s['cpu_usage_percent_maquina'], 1)}%"
    )
    atalhos = "q sair · l logs · e explicar job atual · r reconectar"

    conexao = s.get("connection_state", "desconectado")
    cor_conexao = CORES_CONEXAO.get(conexao, "white")

    # Grid 1b: HASHRATE em destaque à esquerda; ENERGIA + RESULTADOS lado a
    # lado à direita, com TRABALHO embaixo ocupando a largura toda. ENERGIA/
    # RESULTADOS em `Table.grid` (não `Columns`) pra dividir a linha meio a
    # meio de verdade — `Columns` dimensiona pelo conteúdo e deixava sobrar
    # um vão vazio à direita em vez de esticar os dois painéis.
    linha_energia_resultados = Table.grid(expand=True)
    linha_energia_resultados.add_column(ratio=1)
    linha_energia_resultados.add_column(ratio=1)
    linha_energia_resultados.add_row(_secao_energia(s), _secao_resultados(s))
    coluna_direita = Group(linha_energia_resultados, _secao_trabalho(s))
    grade = Table.grid(expand=True)
    grade.add_column(ratio=1)
    grade.add_column(ratio=2)
    grade.add_row(_secao_hashrate(s), coluna_direita)

    itens: list[RenderableType] = [grade]
    if mostrar_progresso:
        progresso = progresso_bloco_percent(s["hashes_total"], s["network_difficulty"])
        itens.append(f"{_barra(progresso / 100)} progresso até o bloco: {progresso:.2e}%")
    itens.append(rodape)
    itens.append(f"[dim]{atalhos}[/dim]")

    corpo = Group(*itens)
    titulo = f"quixote — public-pool.io — [{cor_conexao}]{conexao.upper()}[/{cor_conexao}]"
    return Panel(corpo, title=titulo, border_style=COR_BORDA_NEUTRA)


# --- cliente IPC ---


class _EstadoCliente:
    """Guarda só o snapshot mais recente — troca de referência é atômica no GIL."""

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None


def _cliente_ipc(estado: _EstadoCliente, parar: threading.Event, sock_path: Path) -> None:
    while not parar.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(sock_path))
                arquivo = sock.makefile("r")
                for linha in arquivo:
                    if parar.is_set():
                        return
                    estado.snapshot = json.loads(linha)
        except (OSError, json.JSONDecodeError):
            pass
        if not parar.is_set():
            time.sleep(RECONNECT_INTERVAL_SECONDS)


# --- teclado (I/O de terminal, verificado manualmente com um pty de verdade) ---


def _comando_pager(pager: str, log_file: str) -> list[str]:
    """Monta o comando do pager — `less` ganha `+G` (vai direto pro fim do arquivo)."""
    if pager == "less":
        return [pager, "+G", log_file]
    return [pager, log_file]


@contextlib.contextmanager
def _modo_cbreak() -> Any:
    """Terminal em modo cbreak (tecla chega sem esperar Enter), restaurado ao sair.

    Só faz sentido com um terminal de verdade — `stdin` redirecionado (pipe,
    arquivo) não suporta `termios`, e nesse caso o painel funciona igual a
    antes: sem leitura de teclado, só Ctrl+C.
    """
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def _esperar_tecla_ou_intervalo(interativo: bool, timeout: float) -> str | None:
    """Espera até `timeout`s por uma tecla; `None` se não for interativo ou não vier nada."""
    if not interativo:
        time.sleep(timeout)
        return None
    prontos, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if prontos else None


def _abrir_logs(live: Live, log_file: str | None) -> None:
    """Suspende o painel (sai do alternate screen) pra mostrar os logs, depois volta."""
    live.stop()
    try:
        if not log_file:
            print("\nLOG_FILE não configurado em .env — nada pra mostrar.")
            input("Pressione Enter pra voltar ao painel...")
        else:
            pager = os.environ.get("PAGER", "less")
            try:
                subprocess.run(_comando_pager(pager, log_file), check=False)
            except FileNotFoundError:
                print(f"\n$PAGER={pager!r} não encontrado.")
                input("Pressione Enter pra voltar ao painel...")
    finally:
        live.start(refresh=True)


def run(sock_path: Path = DEFAULT_SOCKET_PATH) -> None:
    """Ponto de entrada do `quixote top`: conecta e renderiza até `q` ou Ctrl+C."""
    repo_root = Path(__file__).resolve().parents[2]
    env = envfile.read_env(repo_root)
    refresh_hz = envfile.require_int(env, "REFRESH_HZ")
    mostrar_progresso = envfile.require_bool(env, "SHOW_PROGRESS_JOKE")
    log_file = env.get("LOG_FILE") or None

    estado = _EstadoCliente()
    parar = threading.Event()
    thread = threading.Thread(
        target=_cliente_ipc, args=(estado, parar, sock_path), daemon=True, name="ipc-cliente"
    )
    thread.start()

    console = Console()
    try:
        with (
            _modo_cbreak() as interativo,
            Live(
                render(None, mostrar_progresso),
                console=console,
                refresh_per_second=refresh_hz,
                screen=True,
            ) as live,
        ):
            while True:
                tecla = _esperar_tecla_ou_intervalo(interativo, 1 / refresh_hz)
                if tecla and tecla.lower() == "q":
                    break
                if tecla and tecla.lower() == "l":
                    _abrir_logs(live, log_file)
                    continue
                live.update(render(estado.snapshot, mostrar_progresso))
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
