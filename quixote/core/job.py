"""Modelo de dados de um job, de uma share encontrada e de um bloco encontrado.

Isola o parsing do protocolo Stratum (formato dos campos de `mining.notify`)
da camada de rede, para que a conversão possa ser testada sem socket nenhum.
Segue a mesma convenção de `core.hashing` e `core.merkle`: hashes ficam
guardados na ordem interna (little-endian corrida), pronta para concatenar
ou colocar direto no header.
"""

from dataclasses import dataclass
from typing import Any

from quixote.core.hashing import swap_endian_words


@dataclass(frozen=True)
class Job:
    """Um job recebido via `mining.notify`.

    Attributes:
        job_id: identificador do job, usado depois em `mining.submit`.
        prev_hash: hash do bloco anterior, 32 bytes, ordem interna.
        coinb1: primeira metade da coinbase, hex, como veio do pool.
        coinb2: segunda metade da coinbase, hex, como veio do pool.
        merkle_branch: hashes irmãos da árvore de merkle, ordem interna,
            na ordem em que o pool os mandou.
        version: versão do bloco.
        nbits: dificuldade em formato compacto.
        ntime: timestamp sugerido pelo pool. Usado literalmente, sem
            rolagem — alguns pools permitem rolar o ntime numa janela, mas
            isso só adiciona complexidade sem necessidade aqui.
        clean_jobs: se `True`, todo trabalho em andamento deve ser
            descartado imediatamente.
    """

    job_id: str
    prev_hash: bytes
    coinb1: str
    coinb2: str
    merkle_branch: list[bytes]
    version: int
    nbits: int
    ntime: int
    clean_jobs: bool

    @classmethod
    def from_notify(cls, params: list[Any]) -> "Job":
        """Constrói um Job a partir dos params crus de `mining.notify`.

        Args:
            params: lista na ordem exata do Stratum: `job_id, prevhash,
                coinb1, coinb2, merkle_branch, version, nbits, ntime,
                clean_jobs`.

        Returns:
            O `Job` com os campos já convertidos para os tipos internos.
        """
        job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs = params
        return cls(
            job_id=job_id,
            prev_hash=bytes.fromhex(swap_endian_words(prevhash)),
            coinb1=coinb1,
            coinb2=coinb2,
            merkle_branch=[bytes.fromhex(branch) for branch in merkle_branch],
            version=int(version, 16),
            nbits=int(nbits, 16),
            ntime=int(ntime, 16),
            clean_jobs=bool(clean_jobs),
        )


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Lê um `CompactSize` (varint do protocolo Bitcoin) a partir de `offset`.

    Returns:
        `(valor, próximo_offset)`.
    """
    primeiro = data[offset]
    if primeiro < 0xFD:
        return primeiro, offset + 1
    tamanhos = {0xFD: 2, 0xFE: 4, 0xFF: 8}
    tamanho = tamanhos[primeiro]
    valor = int.from_bytes(data[offset + 1 : offset + 1 + tamanho], "little")
    return valor, offset + 1 + tamanho


def parse_coinbase_height(coinb1: str) -> int | None:
    """Extrai a altura do bloco da coinbase via BIP34.

    `coinb1` é só o prefixo da transação coinbase (termina antes do
    extranonce ser inserido), mas isso basta: por BIP34 a altura é o
    primeiro item empurrado na `scriptSig`, então já está inteira dentro
    de `coinb1` mesmo quando o resto da `scriptSig` (tag do pool, etc.)
    continua em `coinb2`.

    Estrutura percorrida (todos os campos antes da `scriptSig` têm
    tamanho fixo ou variante padrão do protocolo): versão (4 bytes) +
    contador de inputs (varint) + txid anterior (32 bytes, zerado numa
    coinbase) + índice do output anterior (4 bytes) + tamanho da
    `scriptSig` (varint) + `scriptSig` (começa com um push-opcode de 1
    byte seguido dos bytes da altura em little-endian, CScriptNum).

    Nunca lança — retorna `None` para qualquer estrutura inesperada.

    Args:
        coinb1: primeira metade da coinbase, hex, como veio do pool.

    Returns:
        Altura do bloco, ou `None` se não for possível extrair.
    """
    try:
        data = bytes.fromhex(coinb1)
        offset = 4  # versão
        _, offset = read_varint(data, offset)  # contador de inputs
        offset += 32 + 4  # txid anterior + índice do output anterior
        _, offset = read_varint(data, offset)  # tamanho da scriptSig
        push_len = data[offset]
        if not 1 <= push_len <= 75:
            return None
        altura_bytes = data[offset + 1 : offset + 1 + push_len]
        if len(altura_bytes) != push_len:
            return None
        return int.from_bytes(altura_bytes, "little")
    except (ValueError, IndexError, KeyError):
        return None


@dataclass(frozen=True)
class Share:
    """Uma solução candidata, pronta para `mining.submit`.

    Attributes:
        worker: nome completo `endereco.worker` usado na autorização.
        job_id: job ao qual esta share se refere.
        extranonce2: extranonce2 usado para montar a coinbase desta share.
        ntime: exatamente o ntime do job, sem rolagem.
        nonce: nonce que satisfez o target da pool.
    """

    worker: str
    job_id: str
    extranonce2: bytes
    ntime: int
    nonce: int

    def to_submit_params(self) -> list[str]:
        """Monta os params de `mining.submit`, todos em hex.

        Confirmado contra o public-pool.io real: o pool rejeita com
        "Invalid ntime" se `ntime`/`nonce` forem mandados como o valor
        empacotado little-endian (o formato usado dentro do
        header, via `core.hashing.serialize_header`). O que ele espera é o
        hex "plano" do inteiro — a mesma string, byte a byte, que veio em
        `mining.notify` para `ntime` (ver `Job.from_notify`, que faz
        `int(ntime_hex, 16)`). `nonce` segue a mesma convenção por
        analogia com `version`/`nbits`/`ntime`, que já vêm assim do pool.

        Returns:
            `[worker, job_id, extranonce2, ntime, nonce]`, com `ntime` e
            `nonce` em hex "plano" (`f"{valor:08x}"`), não little-endian.
        """
        return [
            self.worker,
            self.job_id,
            self.extranonce2.hex(),
            f"{self.ntime:08x}",
            f"{self.nonce:08x}",
        ]


@dataclass(frozen=True)
class BlockCandidate:
    """Tudo que é necessário para remontar e propagar um bloco encontrado.

    Existe por um motivo operacional, não estético: quem propaga o bloco é o
    pool, e a única confirmação é o `{"result": true}` do `mining.submit`. Se
    o envio falhar — socket caindo no instante exato, pool fora do ar — o
    trabalho de 4,89 × 10¹⁰ anos-máquina não pode sobrar só numa linha de log
    rotativo. Com os campos abaixo gravados em disco, o header de 80 bytes é
    reconstruível campo a campo e o bloco pode ser submetido à mão por
    qualquer nó (`submitblock`), desde que a coinbase e as transações do
    template sejam recuperadas junto com o pool.

    Todos os campos de bytes ficam em hexadecimal, na ordem em que entram no
    header (little-endian corrida, a convenção de `core.hashing`) — exceto
    `block_hash_display`, invertido, que é a forma mostrada por explorador de
    bloco e a única forma que serve para procurar o bloco depois.

    Attributes:
        found_at: `time.time()` do achado.
        job_id: job do `mining.notify` que originou o header.
        version: campo `version` do header.
        prev_hash: hash do bloco anterior, 32 bytes, ordem interna.
        coinb1: primeira metade da coinbase, como veio do pool.
        coinb2: segunda metade da coinbase, como veio do pool.
        merkle_branch: ramos de merkle, ordem interna, na ordem do pool.
        nbits: dificuldade compacta do header.
        ntime: timestamp do header, exatamente o que o pool mandou.
        extranonce1: extranonce fixo desta sessão Stratum.
        extranonce2: extranonce variável usado nesta coinbase.
        nonce: nonce que satisfez o target da rede.
        coinbase: transação coinbase completa e montada.
        merkle_root: merkle root resultante, 32 bytes, ordem interna.
        header: os 80 bytes exatos do header.
        block_hash_display: `sha256d(header)` invertido, forma de exibição.
    """

    found_at: float
    job_id: str
    version: int
    prev_hash: str
    coinb1: str
    coinb2: str
    merkle_branch: list[str]
    nbits: int
    ntime: int
    extranonce1: str
    extranonce2: str
    nonce: int
    coinbase: str
    merkle_root: str
    header: str
    block_hash_display: str
