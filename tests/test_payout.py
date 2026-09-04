"""Conferência de para quem a coinbase paga (core/payout.py).

No Stratum v1 quem monta a coinbase é o pool, e o `BTC_ADDRESS` da
configuração viaja só como nome de usuário no `mining.authorize`. Estes
testes travam as duas metades da conferência: endereço → `scriptPubKey`, e
`scriptPubKey` → quanto a coinbase paga a ele.

Vetores de endereço vêm do BIP173/BIP350 e da documentação do base58check —
nenhum endereço deste projeto ou do usuário aparece aqui.
"""

import pytest

from quixote.core.merkle import build_coinbase
from quixote.core.payout import address_to_script_pubkey, coinbase_outputs, coinbase_payout_to

EXTRANONCE1 = "aa11bb22"
EXTRANONCE2 = "ccddeeff"

# BIP173, seção "Examples": P2WPKH e P2WSH de mainnet
P2WPKH = (
    "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
    "0014751e76e8199196d454941c45d1b3a323f1433bd6",
)
P2WSH = (
    "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
    "00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262",
)
# BIP350: witness versão 1 (taproot) usa bech32m, constante de checksum outra
P2TR = (
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
    "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
)
P2PKH = ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac")
P2SH = ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87")


def _coinbase(*saidas: tuple[bytes, int]) -> bytes:
    """Monta uma coinbase bem formada com as saídas pedidas.

    Reproduz a divisão real do Stratum: `coinb1` termina no meio da
    `scriptSig` (8 bytes de extranonce entram ali) e `coinb2` começa no
    `sequence`.

    Args:
        saidas: pares `(scriptPubKey, valor em satoshis)`.

    Returns:
        Bytes da coinbase montada por `build_coinbase`.
    """
    coinb1 = "01000000" + "01" + "00" * 32 + "ffffffff" + "08"
    coinb2 = "ffffffff" + f"{len(saidas):02x}"
    for script, valor in saidas:
        coinb2 += valor.to_bytes(8, "little").hex() + f"{len(script):02x}" + script.hex()
    coinb2 += "00000000"  # locktime
    return build_coinbase(coinb1, EXTRANONCE1, EXTRANONCE2, coinb2)


@pytest.mark.parametrize("address,script_hex", [P2WPKH, P2WSH, P2TR, P2PKH, P2SH])
def test_endereco_vira_o_script_pubkey_certo(address, script_hex):
    assert address_to_script_pubkey(address).hex() == script_hex


@pytest.mark.parametrize(
    "address,pedaco_do_erro",
    [
        ("", "vazio"),
        ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "mainnet"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", "checksum"),
        ("bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "maiúsculas"),
        ("mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn", "testnet"),
        ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3", "checksum"),
    ],
)
def test_endereco_invalido_e_recusado_com_motivo(address, pedaco_do_erro):
    """Falhar na partida é de graça; descobrir no dia do bloco não tem volta."""
    with pytest.raises(ValueError, match=pedaco_do_erro):
        address_to_script_pubkey(address)


def test_soma_o_que_a_coinbase_paga_ao_script_pedido():
    nosso = address_to_script_pubkey(P2WPKH[0])
    coinbase = _coinbase((nosso, 317_224_234))

    assert coinbase_payout_to(coinbase, nosso) == 317_224_234


def test_ignora_as_outras_saidas_inclusive_o_compromisso_de_witness():
    """A coinbase real do public-pool tem duas saídas: o pagamento e um
    OP_RETURN de 38 bytes com o compromisso de witness (BIP141), de valor
    zero. A segunda não pode entrar na conta nem confundir o percurso."""
    nosso = address_to_script_pubkey(P2WPKH[0])
    witness_commitment = bytes.fromhex("6a24aa21a9ed" + "11" * 32)
    coinbase = _coinbase((nosso, 312_500_000), (witness_commitment, 0))

    assert coinbase_payout_to(coinbase, nosso) == 312_500_000


def test_paga_outro_endereco_devolve_zero():
    """O caso que para a mineração: a coinbase é válida e não é nossa."""
    nosso = address_to_script_pubkey(P2WPKH[0])
    alheio = address_to_script_pubkey(P2PKH[0])
    coinbase = _coinbase((alheio, 312_500_000))

    assert coinbase_payout_to(coinbase, nosso) == 0


def test_coinbase_ilegivel_devolve_none_em_vez_de_zero():
    """`None` e `0` levam ao mesmo lugar (parar), mas dizem coisas
    diferentes: "não sei ler" não é "não é seu"."""
    nosso = address_to_script_pubkey(P2WPKH[0])

    assert coinbase_payout_to(b"", nosso) is None
    assert coinbase_payout_to(bytes.fromhex("01000000" + "01" + "00" * 10), nosso) is None
    # tamanho de script maior que o que sobrou de transação
    truncada = bytes.fromhex(
        "01000000"
        + "01"
        + "00" * 32
        + "ffffffff"
        + "00"
        + "ffffffff"
        + "01"
        + "00" * 8
        + "43"
        + "4104ac"
    )
    assert coinbase_payout_to(truncada, nosso) is None


def test_coinbase_outputs_devolve_os_pares_na_ordem_da_transacao():
    """Percurso único das saídas: `coinbase_payout_to` soma sobre ele e o
    `--explain` lista as saídas uma a uma, sem um segundo parser."""
    nosso = address_to_script_pubkey(P2WPKH[0])
    witness_commitment = bytes.fromhex("6a24aa21a9ed" + "11" * 32)
    coinbase = _coinbase((nosso, 312_500_000), (witness_commitment, 0))

    assert coinbase_outputs(coinbase) == [(312_500_000, nosso), (0, witness_commitment)]


def test_coinbase_outputs_devolve_none_em_transacao_ilegivel():
    assert coinbase_outputs(b"") is None
    assert coinbase_outputs(bytes.fromhex("01000000" + "01" + "00" * 10)) is None
