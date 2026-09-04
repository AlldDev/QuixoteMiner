"""Conferência de para quem a coinbase do pool está pagando.

O Stratum v1 não tem campo de endereço: o pool monta a transação coinbase e
manda ela partida em duas (`coinb1`/`coinb2`), e o minerador só encaixa o
extranonce no meio e hasheia. O `BTC_ADDRESS` da configuração viaja apenas
como nome de usuário no `mining.authorize` — é o pool que decide, sozinho,
qual `scriptPubKey` vai na saída que recebe o subsídio.

Isso significa que um minerador solo que não decodifica a coinbase não tem
como saber se está trabalhando para si mesmo. Este módulo fecha essa lacuna:
converte o endereço configurado no `scriptPubKey` correspondente e soma
quanto a coinbase montada paga exatamente àquele script.

O que se confere aqui é **destino**, não recebimento: nada é pago sem bloco, e
o valor que aparece é o subsídio + taxas do template do job atual, que o pool
remonta a cada `mining.notify`.

Só stdlib. Bech32/bech32m saem do BIP173 e do BIP350; base58check usa o
mesmo double-SHA256 de `core.hashing`.
"""

import hashlib

from quixote.core.job import read_varint

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_HRP_MAINNET = "bc"
BECH32_CHECKSUM_CONST = 1
"""Constante final do polynomial para bech32 (endereço de witness versão 0)."""
BECH32M_CHECKSUM_CONST = 0x2BC830A3
"""Constante final para bech32m (witness versão 1 ou maior, BIP350)."""

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_VERSION_P2PKH_MAINNET = 0x00
BASE58_VERSION_P2SH_MAINNET = 0x05

OP_0 = 0x00
OP_DUP = 0x76
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_HASH160 = 0xA9
OP_CHECKSIG = 0xAC


def _bech32_polymod(values: list[int]) -> int:
    """Função de checksum do BIP173, aplicada sobre valores de 5 bits."""
    geradores = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        topo = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for bit, gerador in enumerate(geradores):
            if (topo >> bit) & 1:
                checksum ^= gerador
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    """Expande o prefixo humano na forma que o checksum consome."""
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits_5_to_8(values: list[int]) -> bytes:
    """Reagrupa valores de 5 bits em bytes de 8 bits.

    Args:
        values: dígitos de 5 bits, sem o checksum.

    Returns:
        Os bytes correspondentes.

    Raises:
        ValueError: se sobrar padding diferente de zero (endereço inválido).
    """
    acumulador = 0
    bits = 0
    saida = bytearray()
    for value in values:
        acumulador = (acumulador << 5) | value
        bits += 5
        if bits >= 8:
            bits -= 8
            saida.append((acumulador >> bits) & 0xFF)
    if bits >= 5 or ((acumulador << (8 - bits)) & 0xFF) != 0:
        raise ValueError("padding inválido no endereço bech32")
    return bytes(saida)


def _script_pubkey_de_bech32(address: str) -> bytes:
    """Converte um endereço bech32/bech32m de mainnet no seu `scriptPubKey`."""
    minusculo = address.lower()
    if minusculo != address and address.upper() != address:
        raise ValueError("endereço bech32 não pode misturar maiúsculas e minúsculas")
    hrp, separador, dados = minusculo.rpartition("1")
    if not separador or len(dados) < 7:
        raise ValueError("endereço bech32 sem separador ou muito curto")
    if hrp != BECH32_HRP_MAINNET:
        raise ValueError(f"endereço bech32 não é de mainnet (prefixo {hrp!r}, esperado 'bc')")
    if any(c not in BECH32_CHARSET for c in dados):
        raise ValueError("endereço bech32 com caractere fora do alfabeto")

    digitos = [BECH32_CHARSET.index(c) for c in dados]
    witness_version = digitos[0]
    esperado = BECH32_CHECKSUM_CONST if witness_version == 0 else BECH32M_CHECKSUM_CONST
    if _bech32_polymod(_bech32_hrp_expand(hrp) + digitos) != esperado:
        raise ValueError("checksum do endereço bech32 não confere")

    programa = _convert_bits_5_to_8(digitos[1:-6])
    if witness_version == 0 and len(programa) not in (20, 32):
        raise ValueError("witness versão 0 precisa de programa de 20 ou 32 bytes")
    if not 2 <= len(programa) <= 40:
        raise ValueError("programa de witness fora da faixa de 2 a 40 bytes")
    if witness_version > 16:
        raise ValueError("witness versão acima de 16 não existe")

    # OP_0 para versão 0, OP_1..OP_16 (0x51..0x60) para as demais
    opcode = OP_0 if witness_version == 0 else 0x50 + witness_version
    return bytes([opcode, len(programa)]) + programa


def _script_pubkey_de_base58(address: str) -> bytes:
    """Converte um endereço base58check de mainnet no seu `scriptPubKey`."""
    numero = 0
    for caractere in address:
        if caractere not in BASE58_ALPHABET:
            raise ValueError("endereço base58 com caractere fora do alfabeto")
        numero = numero * 58 + BASE58_ALPHABET.index(caractere)
    zeros_a_esquerda = len(address) - len(address.lstrip("1"))
    corpo = b"\x00" * zeros_a_esquerda + numero.to_bytes((numero.bit_length() + 7) // 8, "big")
    if len(corpo) != 25:
        raise ValueError("endereço base58 não tem os 25 bytes de versão + hash160 + checksum")

    carga, checksum = corpo[:21], corpo[21:]
    if hashlib.sha256(hashlib.sha256(carga).digest()).digest()[:4] != checksum:
        raise ValueError("checksum do endereço base58 não confere")

    versao, hash160 = carga[0], carga[1:]
    if versao == BASE58_VERSION_P2PKH_MAINNET:
        return (
            bytes([OP_DUP, OP_HASH160, len(hash160)])
            + hash160
            + bytes([OP_EQUALVERIFY, OP_CHECKSIG])
        )
    if versao == BASE58_VERSION_P2SH_MAINNET:
        return bytes([OP_HASH160, len(hash160)]) + hash160 + bytes([OP_EQUAL])
    raise ValueError(f"versão de endereço base58 {versao:#04x} não é P2PKH nem P2SH de mainnet")


def address_to_script_pubkey(address: str) -> bytes:
    """Converte um endereço Bitcoin de mainnet no `scriptPubKey` que o paga.

    Aceita bech32/bech32m (`bc1...`) e base58check (`1...` e `3...`), sempre
    conferindo o checksum e a rede — um endereço de testnet (`tb1`, `m`, `n`,
    `2`) é recusado, porque minerar mainnet pagando num endereço de testnet
    torna a recompensa irrecuperável.

    Args:
        address: endereço como veio da configuração.

    Returns:
        Os bytes do `scriptPubKey`.

    Raises:
        ValueError: endereço vazio, com checksum errado, de outra rede ou de
            um tipo não reconhecido.
    """
    if not address:
        raise ValueError("endereço vazio")
    # base58 e bech32 são distinguíveis pelo primeiro caractere: só endereço
    # base58 de mainnet começa em "1" (P2PKH) ou "3" (P2SH), e nenhum bech32
    # começa por dígito. O resto vai pro decodificador bech32, que recusa com
    # mensagem específica o que não for prefixo "bc".
    if address[0] in ("1", "3"):
        return _script_pubkey_de_base58(address)
    if address[0] in ("m", "n", "2"):
        raise ValueError("endereço base58 de testnet/regtest, não de mainnet")
    return _script_pubkey_de_bech32(address)


def coinbase_outputs(coinbase: bytes) -> list[tuple[int, bytes]] | None:
    """Percorre as saídas da coinbase montada e devolve `(valor, scriptPubKey)`.

    Percurso único das saídas do projeto: `coinbase_payout_to` soma sobre o
    resultado daqui e o modo didático (`ui.explain`) lista as saídas uma a uma,
    sem um segundo parser.

    Recebe a transação como `core.merkle.build_coinbase` devolve — a mesma de
    que sai o txid, portanto sempre a serialização **sem** witness.

    Estrutura percorrida: versão (4 bytes) + contador de inputs (varint) +
    para cada input `txid` anterior (32) + índice (4) + tamanho da `scriptSig`
    (varint) + `scriptSig` + `sequence` (4) + contador de outputs (varint) +
    para cada output valor em satoshis (8, little-endian) + tamanho do
    `scriptPubKey` (varint) + `scriptPubKey`.

    Nunca lança: estrutura inesperada devolve `None`, para que quem chama
    trate "não sei ler" diferente de "não tem saída nenhuma".

    Args:
        coinbase: transação coinbase completa, como bytes.

    Returns:
        Lista de `(satoshis, scriptPubKey)` na ordem da transação, ou `None`
        se a transação não puder ser percorrida.
    """
    try:
        offset = 4  # versão
        quantidade_inputs, offset = read_varint(coinbase, offset)
        if quantidade_inputs == 0:
            # marcador de serialização segwit; a coinbase do Stratum nunca vem
            # nessa forma (o txid depende da serialização sem witness)
            return None
        for _ in range(quantidade_inputs):
            offset += 32 + 4
            tamanho_script_sig, offset = read_varint(coinbase, offset)
            offset += tamanho_script_sig + 4  # scriptSig + sequence

        quantidade_outputs, offset = read_varint(coinbase, offset)
        saidas: list[tuple[int, bytes]] = []
        for _ in range(quantidade_outputs):
            valor_bytes = coinbase[offset : offset + 8]
            if len(valor_bytes) != 8:
                return None
            valor = int.from_bytes(valor_bytes, "little")
            offset += 8
            tamanho_script, offset = read_varint(coinbase, offset)
            script = coinbase[offset : offset + tamanho_script]
            if len(script) != tamanho_script:
                return None
            offset += tamanho_script
            saidas.append((valor, script))
        if offset + 4 > len(coinbase):  # locktime
            return None
        return saidas
    except (IndexError, KeyError, ValueError):
        return None


def coinbase_payout_to(coinbase: bytes, script_pubkey: bytes) -> int | None:
    """Soma quantos satoshis a coinbase paga a um `scriptPubKey` específico.

    O valor é o subsídio + as taxas **deste template**, montado pelo pool para
    este job: não é saldo nem recebimento, e o pool remonta o template a cada
    `mining.notify`. Só existe de verdade se um hash deste job bater o target
    da rede.

    Args:
        coinbase: transação coinbase completa, como bytes.
        script_pubkey: script de saída procurado.

    Returns:
        Total em satoshis pago àquele script (`0` se nenhuma saída paga a
        ele), ou `None` se a transação não puder ser percorrida.
    """
    saidas = coinbase_outputs(coinbase)
    if saidas is None:
        return None
    return sum(valor for valor, script in saidas if script == script_pubkey)
