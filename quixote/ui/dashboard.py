"""Painel `rich` ao vivo: cliente do socket IPC do daemon.

Processo separado do daemon — só lê `SharedState.to_dict()` via
`telemetry.ipc`, nunca minera nem toca na conexão Stratum. Fechar o painel
não para a mineração.

Formatação numérica em padrão pt-BR (vírgula decimal, ponto de milhar) em
todo o painel, por decisão explícita do usuário, consistente com o resto
da interface já em português. `q` (sair), `l` (abrir os logs) e `e`
(explicar o job atual) têm handler de teclado de verdade (modo cbreak do
terminal, só quando `stdin` é um tty). A explicação do job é calculada no
daemon a cada `mining.notify` (`ui.explain.montar_explicacao_job`) e viaja
no mesmo snapshot já empurrado por `telemetry.ipc` — não precisou de IPC
bidirecional porque a explicação é determinística por job (sempre usa
extranonce2 contador 0), não depende do nonce em andamento. Não há atalho
de reconexão: esse sim exigiria IPC bidirecional de verdade, hoje o socket
só empurra estado do servidor pro cliente.
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
import textwrap
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
from rich.text import Text

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
COR_SISTEMA = "#e0af68"
COR_EXPLICAR = "#f7768e"  # cartão de destaque do atalho `e`, toma a tela toda enquanto ativo
COR_LABEL = "#565f89"  # rótulo de todo campo label/valor, sempre este tom — só o valor muda de cor
COR_NONCE = "#7dcfff"  # único valor com cor própria no mockup; era do extranonce2, passou pro nonce
COR_DESTINO_OK = "#9ece6a"
COR_DESTINO_ERRADO = "#f7768e"

SPARKLINE_NIVEIS = "▁▂▃▄▅▆▇█"

ALTURA_CARTAO_PADRAO = 5  # 3 linhas de conteúdo + 2 de borda (SESSÃO/HISTÓRICO/ENERGIA/TRABALHO)
ALTURA_COLUNA_DIREITA = ALTURA_CARTAO_PADRAO * 2  # duas linhas de dois cartões
# ponytail: número fixo, calculado a partir da estrutura atual dos 4 cartões da
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


def nonce_espaco_percent(nonce: int) -> float:
    """% do espaço de nonce já varrido no job atual.

    Cada `extranonce2` tem `2**32` nonces possíveis, e `core.hasher.mine_job`
    percorre esse espaço em ordem. A 350 KH/s um espaço inteiro levaria ~3,4h
    — na prática o job novo chega muito antes e a varredura recomeça do zero,
    então esse número quase nunca passa de alguns por cento. É essa a graça
    de mostrá-lo: deixa ver o tamanho do espaço de busca contra o quanto dele
    dá tempo de olhar.
    """
    return nonce / 2**32 * 100


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


def _linha_larga(markup: str) -> Text:
    """Uma linha de campo fora do grid, pra valor que não cabe numa célula.

    As colunas de `_grid_campos` são compartilhadas por todas as linhas da
    tabela, então um valor longo (uma data completa, o motivo de rejeição que
    o pool mandou) estica a coluna dele e esprememe os rótulos das outras
    linhas — "aceitas" virando "acei…" mesmo sobrando espaço no cartão. Fora
    da tabela, o valor longo só trunca a si mesmo. `no_wrap` mantém uma linha
    só, que é o que segura a altura fixa dos cartões
    (`ALTURA_CARTAO_PADRAO`).

    Só serve pra valor de tamanho imprevisível. Quando dá pra fixar a largura
    do valor na origem (o nonce em hexadecimal, por exemplo), fixar é melhor:
    aí ele cabe no grid e fica alinhado com o resto do cartão.
    """
    texto = Text.from_markup(markup)
    texto.no_wrap = True
    texto.overflow = "ellipsis"
    return texto


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
    """Cartão de destaque do painel: número grande, sparkline e campos alinhados.

    As duas primeiras linhas não são campos rótulo/valor de propósito — são o
    ponto do cartão de destaque: o hashrate instantâneo em corpo grande e o
    histórico recente. O resto sai de `_grid_campos`, igual aos outros cinco
    cartões, com **um par por linha** porque o cartão ocupa só 1/3 da largura.

    A versão anterior concatenava os campos dentro de strings
    (`"média sessão X    capacidade máx Y (Z%)"`), e isso quebrava de duas
    formas: rótulo caindo em coluna diferente conforme a largura do valor
    vizinho, e quebra de linha de verdade em qualquer terminal abaixo de ~200
    colunas — dentro de um cartão de altura travada, a linha que quebra come a
    de baixo. O percentual de capacidade também aparecia solto entre
    parênteses, sem rótulo dizendo o que era; virou o campo `uso`.

    São exatamente 8 linhas de conteúdo (número, sparkline, branco, 4 campos,
    barra), que é `ALTURA_COLUNA_DIREITA` menos as duas de borda. Acrescentar
    campo aqui exige revisar aquela constante.
    """
    alvo = s.get("target_hashrate")
    capacidade = s.get("calibrated_max_hashrate")
    cap_percent = capacidade_percent(s["hashrate_avg"], capacidade)
    uso = "—" if cap_percent is None else format_numero_ptbr(cap_percent, 1) + "%"
    grid = _grid_campos(
        (("alvo", scale_si(alvo, "H/s") if alvo else "—"),),
        (("média sessão", scale_si(s["hashrate_avg"], "H/s")),),
        (("capacidade máx", scale_si(capacidade, "H/s") if capacidade else "não calibrada"),),
        (("uso", uso),),
    )
    spark = _sparkline(s["hashrate_history"])
    corpo = Group(
        f"[bold {COR_HASHRATE}]{scale_si(s['hashrate_instant'], 'H/s')}[/bold {COR_HASHRATE}]",
        spark if spark else "[dim]aguardando histórico...[/dim]",
        "",
        grid,
        # barra de 20 e não BARRA_LARGURA (28): o cartão tem ~30 colunas úteis
        # num terminal de 100 colunas, e a de 28 encostaria na borda
        _linha_larga(_barra((cap_percent or 0.0) / 100, 20)),
    )
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
    """Onde a busca está agora: job, alvos e a posição no espaço de nonce.

    O campo do nonce ficava no `extranonce2`, que é zero permanente por
    construção — `mine_job` só o incrementa quando esgota os 2**32 nonces de
    um espaço, e a 350 KH/s o job novo sempre chega antes (~3,4h).

    O nonce sai em hexadecimal de 8 dígitos, e não em decimal com separador
    de milhar, por causa da largura: em decimal ele cresce de 5 pra 13
    caracteres conforme a varredura avança, e como as colunas do grid são
    compartilhadas por todas as linhas do cartão, o layout inteiro se
    reajustava a cada quadro. 8 dígitos são 8 dígitos do começo ao fim do
    espaço. De quebra é a forma como o nonce existe de verdade: 4 bytes do
    header, que é como qualquer explorador de blocos mostra.

    O `ntime` saiu daqui quando o cartão perdeu a largura total: os seis
    campos não cabem alinhados em ~38 colunas. Ele continua no `--explain`,
    que imprime o header campo a campo.
    """
    altura = s.get("current_block_height")
    nonce = s.get("current_nonce") or 0
    grid = _grid_campos(
        (
            ("job", s["current_job_id"] or "—"),
            ("altura", format_numero_ptbr(altura, 0) if altura is not None else "—"),
        ),
        (
            ("dif pool", scale_si(s["pool_difficulty"])),
            ("dif rede", scale_si(s["network_difficulty"])),
        ),
        (
            ("nonce", f"[{COR_NONCE}]{nonce:08x}[/{COR_NONCE}]"),
            ("espaço", format_numero_ptbr(nonce_espaco_percent(nonce), 2) + "%"),
        ),
    )
    titulo = f"[{COR_TRABALHO}]TRABALHO[/{COR_TRABALHO}]"
    return Panel(grid, title=titulo, border_style=COR_BORDA_NEUTRA)


def _secao_sessao(s: dict[str, Any]) -> Panel:
    """Resultados desde que este processo do daemon subiu.

    Sempre 3 linhas de conteúdo (motivo de rejeição embutido na célula
    "rejeitadas", não numa linha própria) — mesma altura dos outros três
    cartões da direita, pro HASHRATE poder casar com a coluna inteira
    (ver `ALTURA_COLUNA_DIREITA`).
    """
    grid = _grid_campos(
        (("aceitas", str(s["shares_accepted"])), ("rejeitadas", str(s["shares_rejected"]))),
        (
            ("melhor", format_numero_ptbr(s["best_difficulty_session"], 4)),
            ("blocos", str(s["blocks_found"])),
        ),
    )
    # O motivo da última rejeição vem junto do último evento, não colado no
    # contador: é texto do pool, de tamanho imprevisível, e dentro do grid
    # esticava a coluna e truncava os rótulos vizinhos.
    motivo = s.get("last_rejection_reason")
    ultima = _linha_larga(
        f"[{COR_LABEL}]última share[/{COR_LABEL}] "
        f"{format_tempo_relativo(s.get('last_share_timestamp'), time.time())}"
        + (f"  [dim]({motivo})[/dim]" if motivo else "")
    )
    titulo = f"[{COR_RESULTADOS}]SESSÃO[/{COR_RESULTADOS}]"
    return Panel(Group(grid, ultima), title=titulo, border_style=COR_BORDA_NEUTRA)


def _secao_historico(s: dict[str, Any]) -> Panel:
    """Resultados acumulados de todas as execuções (o que sobrevive em `state.json`).

    Existe porque os totais persistidos já viajavam no snapshot e não
    apareciam em lugar nenhum do painel: quem reiniciava o serviço via o
    trabalho de semanas virar zero na tela, mesmo estando gravado em disco.
    """
    grid = _grid_campos(
        (
            ("aceitas", str(s.get("shares_accepted_total", 0))),
            ("rejeitadas", str(s.get("shares_rejected_total", 0))),
        ),
        (
            ("melhor", format_numero_ptbr(s["best_difficulty_ever"], 4)),
            ("blocos", str(s.get("blocks_found_total", 0))),
        ),
    )
    recorde = _linha_larga(
        f"[{COR_LABEL}]recorde em[/{COR_LABEL}] "
        f"{format_timestamp_ptbr(s.get('best_difficulty_ever_timestamp'))}"
    )
    titulo = f"[{COR_RESULTADOS}]HISTÓRICO[/{COR_RESULTADOS}]"
    return Panel(Group(grid, recorde), title=titulo, border_style=COR_BORDA_NEUTRA)


def formatar_destino_recompensa(coinbase_pays_us_satoshis: int | None, tem_job: bool) -> str:
    """Monta a linha de conferência do destino da recompensa.

    No Stratum v1 quem escolhe o destino é o pool: o `coinb2` já traz o
    `scriptPubKey` da saída que recebe o subsídio, e o `BTC_ADDRESS` viaja só
    como nome de usuário no `mining.authorize`. O daemon decodifica o endereço
    configurado e confere, a cada job, se a coinbase paga àquele script
    (`core.payout`) — sem mostrar isso na tela, um desvio de destino seria
    invisível.

    **A linha não mostra valor em BTC, de propósito.** A versão anterior
    imprimia `pagamento CONFERIDO 3,15439929 BTC no seu endereço` e foi lida
    como dinheiro recebido, que é exatamente o que não aconteceu: sem bloco
    encontrado não existe recompensa nenhuma, e aquele número era o subsídio
    + taxas do template do job atual, remontado pelo pool a cada
    `mining.notify` (visto variando 3,142 / 3,154 / 3,161 BTC em minutos).
    O valor vive no log da conferência e no `--explain`, onde há espaço pra
    dizer o que ele é. Não recolocar aqui.

    O veredito vem antes da explicação porque a linha sai na coluna de valor de
    `_grid_campos`, que é `no_wrap` + ellipsis: truncando em terminal estreito,
    perde-se o final, nunca o `CONFERIDO`/`NÃO CONFERE`. O rótulo
    ("recompensa") não vem daqui — é a primeira coluna do grid, como em todo
    campo do painel.

    Args:
        coinbase_pays_us_satoshis: campo homônimo do snapshot — total que a
            coinbase do job atual paga ao endereço configurado, `0` se não
            paga nada e `None` se a coinbase não pôde ser percorrida (ou se
            nenhum job chegou ainda).
        tem_job: se já existe job atual — distingue "ainda não sei" de
            "coinbase ilegível".

    Returns:
        Markup do Rich, já colorido.
    """
    if coinbase_pays_us_satoshis is None and not tem_job:
        return "destino ainda não conferido, aguardando o primeiro job"
    if coinbase_pays_us_satoshis is None:
        return (
            f"destino [{COR_DESTINO_ERRADO}]DESCONHECIDO[/{COR_DESTINO_ERRADO}]"
            " · coinbase deste job ilegível, mineração parada"
        )
    if coinbase_pays_us_satoshis == 0:
        return (
            f"destino [{COR_DESTINO_ERRADO}]NÃO CONFERE[/{COR_DESTINO_ERRADO}]"
            " · coinbase deste job paga outro endereço, mineração parada"
        )
    return (
        f"destino [{COR_DESTINO_OK}]CONFERIDO[/{COR_DESTINO_OK}]"
        " · a coinbase deste job paga seu endereço"
    )


def _secao_sistema(s: dict[str, Any], mostrar_progresso: bool) -> Panel:
    """Cartão de largura total: destino da recompensa, progresso, uptime e CPU.

    Um `_grid_campos` só, um par por linha — a mesma gramática dos outros
    cartões. Antes eram três formas empilhadas (uma `_linha_larga`, uma
    f-string com a barra e o rótulo no meio, um grid de três pares numa linha)
    e nenhum rótulo alinhava com o de cima.

    Um par por linha resolve o problema que motivou `_linha_larga` sem precisar
    dela: a coluna do valor é a última do grid, então o texto longo da
    recompensa só estica a si mesmo, e `_grid_campos` já põe `no_wrap` +
    `ellipsis` nessa coluna — que é a propriedade de que
    `formatar_destino_recompensa` depende pra sobreviver ao truncamento.

    A conferência de destino mora aqui, e não no cartão TRABALHO, por dois
    motivos: os quatro cartões da direita têm altura fixa de 3 linhas de
    conteúdo (`ALTURA_CARTAO_PADRAO`), e o texto pode ficar longo — este
    cartão é o único de largura total e de altura variável.
    """
    linhas: list[tuple[tuple[str, str], ...]] = [
        (
            (
                "recompensa",
                formatar_destino_recompensa(
                    s.get("coinbase_pays_us_satoshis"), bool(s.get("current_job_id"))
                ),
            ),
        )
    ]
    if mostrar_progresso:
        progresso = progresso_bloco_percent(s["hashes_total"], s["network_difficulty"])
        # a notação científica tem um ponto decimal só (o `e-16` não é ponto),
        # então o replace basta pra deixar o número em pt-BR como o resto do painel
        percent_ptbr = f"{progresso:.2e}".replace(".", ",")
        linhas.append((("progresso", f"{_barra(progresso / 100)}  {percent_ptbr}% até o bloco"),))
    linhas.append((("uptime", format_uptime(s["uptime_seconds"])),))
    linhas.append((("cpu (núcleo)", format_numero_ptbr(s["cpu_usage_percent"], 1) + "%"),))
    linhas.append((("cpu (máquina)", format_numero_ptbr(s["cpu_usage_percent_maquina"], 1) + "%"),))
    titulo = f"[{COR_SISTEMA}]SISTEMA[/{COR_SISTEMA}]"
    return Panel(_grid_campos(*linhas), title=titulo, border_style=COR_BORDA_NEUTRA)


def _formatar_atalho(tecla: str, acao: str) -> str:
    return f"[bold black on {COR_SISTEMA}] {tecla.upper()} [/bold black on {COR_SISTEMA}] {acao}"


def _linha_de_dois(esquerda: Panel, direita: Panel) -> Table:
    """Dois cartões dividindo a largura meio a meio."""
    linha = Table.grid(expand=True)
    linha.add_column(ratio=1)
    linha.add_column(ratio=1)
    linha.add_row(esquerda, direita)
    return linha


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
    atalhos = "   ".join(
        _formatar_atalho(tecla, acao)
        for tecla, acao in [
            ("q", "sair"),
            ("l", "logs"),
            ("e", "explicar job atual"),
        ]
    )

    conexao = s.get("connection_state", "desconectado")
    cor_conexao = CORES_CONEXAO.get(conexao, "white")

    # Grid 1b: HASHRATE em destaque à esquerda, altura das duas linhas; à
    # direita, dois cartões por linha — SESSÃO + HISTÓRICO em cima, ENERGIA +
    # TRABALHO embaixo. `Table.grid` (não `Columns`) pra dividir cada linha
    # meio a meio de verdade — `Columns` dimensiona pelo conteúdo e deixava
    # sobrar um vão vazio à direita em vez de esticar os dois painéis.
    coluna_direita = Group(
        _linha_de_dois(_secao_sessao(s), _secao_historico(s)),
        _linha_de_dois(_secao_energia(s), _secao_trabalho(s)),
    )
    grade = Table.grid(expand=True)
    grade.add_column(ratio=1)
    grade.add_column(ratio=2)
    grade.add_row(_secao_hashrate(s), coluna_direita)

    itens: list[RenderableType] = [grade, _secao_sistema(s, mostrar_progresso)]
    itens.append(atalhos)

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
    return _ler_byte() if prontos else None


def _ler_byte() -> str:
    """Lê um byte direto do descritor, sem passar pelo buffer do `sys.stdin`.

    `sys.stdin.read(1)` é um `TextIOWrapper`: ele puxa do fd tudo que estiver
    disponível (uma seta chega como os 3 bytes `ESC [ A` de uma vez) e guarda
    o resto num buffer interno em Python. O `select` seguinte olha o fd, que
    já está vazio, e conclui que não veio nada — a sequência de escape ficava
    invisível e a seta era lida como um `ESC` solto. `os.read` no fd mantém
    `select` e leitura falando do mesmo lugar.
    """
    return os.read(sys.stdin.fileno(), 1).decode(errors="ignore")


ESCAPE_PARA_TOKEN = {"A": "up", "B": "down", "5": "pgup", "6": "pgdn"}


def _ler_tecla_navegacao() -> str:
    """Bloqueia até uma tecla, decodificando setas/PageUp/PageDown em tokens.

    Setas e PageUp/PageDown chegam como sequência de escape (`ESC [ A`,
    `ESC [ 5 ~`, etc.), não um caractere só — sem decodificar isso, `_ler`
    devolveria só o `ESC` (ou o `[`/dígito seguinte) e a rolagem pareceria
    travada. A maioria dos terminais (xterm, gnome-terminal, iTerm, kitty,
    alacritty...) mapeia a roda do mouse pra essas mesmas setas quando o
    programa está na alternate screen sem suporte a scroll nativo — então
    decodificar as setas também resolve a rolagem por mouse "de graça".
    Qualquer outra tecla volta como veio, pro chamador decidir (ex.: sair).
    """
    tecla = _ler_byte()
    if tecla != "\x1b":
        return tecla
    prontos, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not prontos:
        return tecla  # ESC sozinho, sem sequência vindo atrás
    if _ler_byte() != "[":
        return tecla
    codigo = _ler_byte()
    if codigo in "56":
        select.select([sys.stdin], [], [], 0.05)
        _ler_byte()  # descarta o '~' final de ESC[5~/ESC[6~
    return ESCAPE_PARA_TOKEN.get(codigo, tecla)


def _avisar_log(console: Console, mensagem: str) -> None:
    """Mostra um aviso de `_abrir_logs` num Panel com a borda do tema, não texto cru.

    O `less` em si (caminho feliz) não pode ser bordado — assume a tela
    inteira — mas os avisos de fallback (sem `LOG_FILE`, `$PAGER` ausente)
    são nosso próprio texto, então ganham a mesma moldura dos outros
    cartões do painel.
    """
    console.print(Panel(mensagem, title="LOGS", border_style=COR_BORDA_NEUTRA))
    input("Pressione Enter pra voltar ao painel...")


def _abrir_logs(live: Live, log_file: str | None) -> None:
    """Suspende o painel (sai do alternate screen) pra mostrar os logs, depois volta."""
    live.stop()
    try:
        console = Console()
        if not log_file:
            _avisar_log(console, "LOG_FILE não configurado em .env — nada pra mostrar.")
        else:
            pager = os.environ.get("PAGER", "less")
            try:
                subprocess.run(_comando_pager(pager, log_file), check=False)
            except FileNotFoundError:
                _avisar_log(console, f"$PAGER={pager!r} não encontrado.")
    finally:
        live.start(refresh=True)


LINHAS_RESERVADAS_EXPLICACAO = 2
"""As 2 linhas de borda do Panel — o rodapé mora *na* borda de baixo, não no corpo."""


def _quebrar_para_largura(linhas: list[str], largura: int) -> list[str]:
    """Quebra cada linha em pedaços de no máximo `largura` colunas, virando linhas próprias.

    Sem isso, uma linha longa (o `coinb1` inteiro em hex, por exemplo) é
    quebrada pelo Rich *dentro* do cartão: o Panel fica mais alto que a tela,
    e o que sobra pra fora é justo a borda de baixo, com o rodapé. Quebrando
    aqui, cada pedaço conta como uma linha rolável — nada se perde e a altura
    do cartão continua exata.
    """
    return [
        pedaco
        for linha in linhas
        for pedaco in (textwrap.wrap(linha, largura, drop_whitespace=False) or [""])
    ]


def _painel_explicacao(linhas: list[str], offset: int, altura_visivel: int) -> Panel:
    """Monta o cartão da explicação com a janela de `altura_visivel` linhas a partir de `offset`.

    O rodapé de ajuda/paginação vai no `subtitle`, que o Rich desenha *sobre*
    a borda de baixo, do mesmo jeito que o título fica sobre a de cima. No
    corpo, ele subia junto com o conteúdo e sumia de vista; na borda, só o
    conteúdo se mexe. `offset` já vem limitado pelo chamador.
    """
    janela = linhas[offset : offset + altura_visivel]
    ajuda = "↑/↓ ou roda do mouse rola · PgUp/PgDn pula página · outra tecla volta"
    # Posição primeiro: o Rich corta o `subtitle` pela largura do cartão, e
    # em terminal estreito é a ajuda que pode ir embora, não onde você está.
    rodape = ajuda
    if len(linhas) > altura_visivel:
        rodape = f"[{offset + 1}-{offset + len(janela)}/{len(linhas)}]    {ajuda}"
    return Panel(
        Text("\n".join(janela), no_wrap=True, overflow="crop"),
        title=f"[{COR_EXPLICAR}]EXPLICAR JOB[/{COR_EXPLICAR}]",
        subtitle=rodape,
        border_style=COR_EXPLICAR,
    )


def _mostrar_explicacao(live: Live, snapshot: dict[str, Any] | None) -> None:
    """Mostra a explicação do job atual num cartão próprio, navegável até sair.

    Diferente de `_abrir_logs`, não sai do Rich/alternate screen — a
    explicação já chega pronta no snapshot (calculada pelo daemon a cada
    job, ver docstring do módulo). Como pode ser mais longa que a tela (e a
    alternate screen não tem scrollback), pagina manualmente: setas/roda do
    mouse rolam linha a linha, PageUp/PageDown rolam página inteira
    (`_ler_tecla_navegacao` decodifica as sequências de escape), qualquer
    outra tecla volta ao painel.
    """
    explicacao = snapshot.get("current_job_explanation") if snapshot else None
    linhas = (explicacao or "aguardando o primeiro job pra explicar...").split("\n")

    offset = 0
    while True:
        altura_visivel = max(1, live.console.size.height - LINHAS_RESERVADAS_EXPLICACAO)
        # 2 de borda + 2 de padding do Panel; recalculado a cada quadro porque
        # o terminal pode ser redimensionado com a explicação aberta.
        roladas = _quebrar_para_largura(linhas, max(20, live.console.size.width - 4))
        offset_max = max(0, len(roladas) - altura_visivel)
        offset = min(offset, offset_max)
        live.update(_painel_explicacao(roladas, offset, altura_visivel))

        tecla = _ler_tecla_navegacao()
        if tecla == "up":
            offset = max(0, offset - 1)
        elif tecla == "down":
            offset = min(offset_max, offset + 1)
        elif tecla == "pgup":
            offset = max(0, offset - altura_visivel)
        elif tecla == "pgdn":
            offset = min(offset_max, offset + altura_visivel)
        else:
            return


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
                if tecla and tecla.lower() == "e":
                    _mostrar_explicacao(live, estado.snapshot)
                    continue
                live.update(render(estado.snapshot, mostrar_progresso))
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
