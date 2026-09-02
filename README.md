# QuixoteMiner

<p align="center">
  <strong>Um minerador solo de Bitcoin para desktop, que sabe que as chances são astronomicamente pequenas.</strong>
</p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="Licença MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/rust-required-orange" alt="Rust obrigatório">
  <a href="#apoie-o-projeto"><img src="https://img.shields.io/badge/₿-apoie_o_projeto-F7931A?logo=bitcoin&logoColor=white" alt="Apoie o projeto com Bitcoin"></a>
</p>

<p align="center">
  <a href="#o-nome">O nome</a> ·
  <a href="#como-funciona">Como funciona</a> ·
  <a href="#shares-vs-bloco">Shares vs. bloco</a> ·
  <a href="#arquitetura">Arquitetura</a> ·
  <a href="#o-teste-do-bloco-125552">O teste do bloco 125552</a> ·
  <a href="#aviso">Aviso</a> ·
  <a href="#rodando">Rodando</a> ·
  <a href="#testes">Testes</a> ·
  <a href="#licença">Licença</a> ·
  <a href="#apoie-o-projeto">Apoie o projeto</a>
</p>

Quixote conecta numa pool de mineração solo real (`public-pool.io`) e minera Bitcoin de verdade , 
com o hashrate travado de propósito em 350 KH/s, consumo de 1 a 4 W, e um painel de terminal que
mostra, sem rodeios, exatamente o quanto essa aposta é desfavorável.

---

## O nome

`quixote`, de Dom Quixote, o cavaleiro que atacava moinhos de vento acreditando serem gigantes. A
analogia é literal: este programa aponta algumas centenas de milhares de hashes por segundo contra
uma rede que soma centenas de exahashes por segundo ,  treze ordens de grandeza de diferença. Em vez
de esconder isso, o projeto assume a futilidade como característica: o painel mostra ao vivo o
quanto de energia (e de chance) está sendo gasto, sem sugerir em nenhum momento que existe alguma
expectativa razoável de lucro.

---

## Como funciona

Um daemon Python conecta via **Stratum v1** (o protocolo que ASICs e pools comerciais usam de
verdade) no `public-pool.io` ,  uma pool desenhada para aceitar hashrate baixo (o público dela é
Bitaxe e NerdMiner), então mostra o worker no dashboard e aceita "shares" com dificuldade baixa em
vez de exigir a dificuldade de milhares que pools grandes pedem.

O ciclo é o mesmo de qualquer minerador Bitcoin de verdade:

1. **`mining.subscribe`/`mining.authorize`**: conecta e se identifica com o endereço BTC.
2. **`mining.notify`**: a pool manda um "job" ,  os ingredientes pra montar um bloco candidato
   (transação coinbase parcial, ramos de merkle, versão, dificuldade da rede, timestamp).
3. **Montagem do header**: o programa monta a transação coinbase completa, calcula o merkle root, e
   monta os 80 bytes do header de bloco.
4. **Busca de nonce**: varia o campo `nonce` (e, quando necessário, o `extranonce2`) calculando
   `sha256d` do header a cada tentativa, comparando o resultado contra dois alvos diferentes ,  o da
   pool (baixo, acontece com frequência) e o da rede (real, extraordinariamente improvável de acontecer).
5. **`mining.submit`**: quando um hash passa no alvo da pool, submete a solução. A resposta
   `{"result": true}` é a prova de que a mineração é real ,  não uma simulação.

O hashrate é **limitado de propósito** por um throttle interno (mede o tempo gasto em lotes de
hashes e dorme o que sobrar), porque o objetivo aqui nunca foi competir por um bloco de verdade , 
é entender o protocolo por dentro, gastando o mínimo de energia possível pra ilustrar o ponto.

Além de minerar, o daemon mede consumo de energia real (via RAPL do processador, quando disponível,
com um modelo estimado como alternativa) e expõe todo esse estado ,  hashrate, shares, dificuldade,
watts, custo em reais ,  para um painel de terminal (`quixote top`) que roda como processo separado,
lendo por um socket Unix local. Fechar o painel não interrompe a mineração.

---

## Shares vs. bloco

O passo 4 de [Como funciona](#como-funciona) compara cada hash contra **dois alvos diferentes**, e a
diferença entre eles é o ponto central deste projeto.

| | Dificuldade | Frequência esperada a 350 KH/s |
|---|---|---|
| Alvo da pool (share) | `1` (`SUGGEST_DIFFICULTY` no `.env`) | horas |
| Alvo da rede (bloco) | ~125,8 T (bloco ~964.942, [coinwarz.com](https://www.coinwarz.com/bitcoin-difficulty), 2026-09-02) | dezenas de bilhões de anos, em expectativa |

A dificuldade da rede é ~1,26 × 10¹⁴ vezes a da pool. Uma share não é "quase um bloco": é uma amostra
independente que só por acaso cairia abaixo de um alvo 14 ordens de grandeza menor. Achar shares com
frequência normal (a cada poucas horas) não aproxima em nada de achar um bloco ,  as duas coisas nem
crescem na mesma escala.

Contando: o número esperado de hashes até um bloco é `dificuldade × 2³² ≈ 5,4 × 10²³`. A 350 KH/s
isso dá uma expectativa da ordem de `5 × 10¹⁰` anos ,  mais que a idade do universo. Dito isso, **isso
é uma média, não uma garantia de nunca**: a busca de nonce é, estatisticamente, uma sequência de
tentativas independentes (como jogar uma moeda), e nada no protocolo impede um resultado extremo
acontecer cedo. É extraordinariamente improvável, não impossível.

Como o `public-pool.io` é uma pool **solo**, a
share não paga nada parcial ,  ela só prova pro pool que este worker está de fato calculando hashes
(telemetria e anti-spam do lado dele, é o que aparece no dashboard público). O prêmio inteiro só
existe se um desses hashes também bater o alvo da rede.

**Quem valida um bloco não é o Quixote.** O daemon nunca monta o bloco completo ,  o job do Stratum
(`mining.notify`) só traz `coinb1`/`coinb2` e o `merkle_branch` (o galho da árvore, não a lista de
transações inteira). Quando um hash passa no alvo da rede, `quixote/core/hasher.py` só loga
`BLOCO ENCONTRADO!` como telemetria local ,  isso é uma afirmação do cliente, não uma prova. A
submissão (`mining.submit`) vai pro pool, e é o nó Bitcoin completo por trás dele que confere o
hash de verdade, monta o bloco inteiro (cabeçalho + coinbase real + as transações que ele escolheu)
e propaga pra rede, onde cada nó completo valida as regras de consenso de novo. Por isso o teste que
importa (veja [Testes](#testes)) exige o `{"result": true}` vindo do pool: é a única confirmação
externa de que algo bateu em algum alvo válido ,  nenhum log local, por mais alto o nível, prova
sozinho.

---

## Arquitetura

```
┌────────────────────────────────────────────────────────┐
│                     quixote (daemon)                   │
│                                                        │
│  ┌────────────┐    Job     ┌────────────┐              │
│  │  Stratum   │──────────▶│   Hasher   │              │
│  │  (thread)  │◀──────────│  (thread)  │              │
│  └─────┬──────┘   Share    └─────┬──────┘              │
│        │                         │                     │
│        │      ┌──────────────────▼──────┐    ┌────────┐│
│        └────▶│   SharedState (lock)    │◀──│ Power  ││
│               └──────────┬──────────────┘    │(thread)││
│                          │                   └────────┘│
│                 ┌────────▼────────┐                    │
│                 │  socket Unix    │                    │
│                 └────────┬────────┘                    │
└──────────────────────────┼─────────────────────────────┘
                           │ JSON por linha, 4x/segundo
                 ┌─────────▼─────────┐
                 │   quixote top     │ -> processo separado
                 │  (painel `rich`)  │
                 └───────────────────┘
```

O daemon precisa rodar sempre, com prioridade e cota de CPU baixas. O painel só existe enquanto
alguém está olhando, e pode ser fechado e reaberto (inclusive de outra sessão SSH) sem afetar a
mineração ,  por isso os dois são processos separados, e não uma única aplicação com uma tela.

---

## O teste do bloco 125552

A parte do protocolo onde praticamente toda implementação caseira erra: **ordem de bytes**.

O bloco 125552 da rede Bitcoin é um vetor de teste canônico, com todos os campos do header e o hash
resultante já conhecidos:

| Campo | Valor |
|---|---|
| `version` | `1` |
| `prevhash` (exibição) | `00000000000008a3a41b85b8b29ad444def299fee21793cd8b9e567eab02cd81` |
| `merkle root` (exibição) | `2b12fcf1b09288fcaff797d71e950e71ae42b91e8bdb2304758dfcffc2b620e3` |
| `ntime` | `1305998791` |
| `nbits` | `0x1a44b9f2` |
| `nonce` | `2504433986` |
| hash do bloco (esperado) | `00000000000000001e8d6829a8a21adc5d38d0a473b144b6765798e61f98bd1d` |

A armadilha: o `prevhash` que um pool manda via Stratum (e o valor que um explorador de blocos
mostra) vem em **ordem de exibição** ,  mas o header de bloco precisa dos bytes de cada hash (32
bytes) na **ordem inversa**, porque é assim que o Bitcoin sempre tratou o resultado de um SHA-256
(como um número little-endian). Monte o header com os hashes na ordem "errada" e o resultado final
não bate com nada ,  sem erro, sem exceção, só um hash que não confere com o que o resto da rede
enxerga.

O mesmo vale, ao contrário, na saída: o `sha256d` do header de 80 bytes produz um hash em ordem
"interna" ,  pra virar o valor que aparece num explorador de blocos, ele também precisa ser invertido
byte a byte.

Esse teste (`sha256d(header) invertido == hash esperado do bloco 125552`) é o primeiro que este
projeto precisa passar, antes de qualquer linha de código de rede ,  se ele falhar, nada mais no
projeto pode estar certo, porque toda a cadeia (coinbase → merkle → header → hash) depende dessa
mesma convenção de bytes.

Quer ver essa montagem acontecer, campo a campo, contra um job real vindo do pool? Rode o daemon com
`--explain` (veja [Rodando](#rodando)) ,  ele imprime a mesma lógica, só que com dados ao vivo em vez
de um vetor fixo.

---

## Aviso

Este projeto **não tem como objetivo dar lucro**, e não deveria ser interpretado como uma tentativa
de investimento. Com 350 KH/s contra uma rede que hoje soma centenas de exahashes por segundo, o
valor esperado de qualquer sessão de mineração é negativo por várias ordens de grandeza ,  o custo de
energia sempre supera, de longe, a chance de encontrar um bloco (detalhe da conta em
[Shares vs. bloco](#shares-vs-bloco)). Essa chance é astronomicamente pequena, não zero: nada no
protocolo impede um resultado extremo acontecer cedo, só é extraordinariamente improvável. O próprio
painel mostra isso sem disfarce: uma barra de progresso "até o bloco" que nunca sai do zero de forma
perceptível.

O propósito é didático: entender o protocolo Stratum, a montagem de um bloco Bitcoin, e o que
significa "prova de trabalho" experimentando com uma implementação real e funcional, não uma
simulação.

---

## Rodando

Configuração do `.env`, criação do ambiente virtual, e instalação como serviço de usuário do
systemd (pra rodar sempre, em segundo plano) estão em **[`contrib/README.md`](./contrib/README.md)**.

Depois de configurado:

```bash
quixote daemon --no-detach --log-level DEBUG    # primeiro plano, log detalhado
quixote daemon --explain                        # explica o primeiro job recebido, depois continua
quixote top                                     # painel ao vivo, processo separado
```

`--log-level TRACE` (abaixo de `DEBUG`) loga a montagem do header a cada job ,  mais verboso que o
dia a dia normal, útil pra acompanhar o protocolo de perto. O laço de busca de nonce em si roda em
Rust (`quixote_native`, ver [`contrib/README.md`](./contrib/README.md)), então não tem mais um log
por hash individual nesse nível.

---

## Testes

```bash
pytest -v -m "not slow"
```

Cobre cada peça isoladamente: os vetores de hash conhecidos (bloco 125552 e outro bloco real,
independente), a montagem de merkle e coinbase, conversão de dificuldade/target, o protocolo
Stratum contra um servidor mock, e o comportamento do throttle.

A prova que importa de verdade, porém, não é nenhum desses ,  é o teste de integração
(`pytest -v -m slow`, roda manualmente porque depende da rede e pode levar até 30 minutos): ele
conecta no pool real e espera a primeira share ser **aceita**. Hashrate aparecendo na tela não prova
nada; uma resposta `{"result": true}` vinda do pool, sim.

---

## Licença

[MIT](./LICENSE)

---

## Apoie o projeto

<p align="center">
  <a href="bitcoin:bc1qtu6nvfjdcujpweazeq8w0et0vs7swmef75nurw">
    <img src="https://img.shields.io/badge/₿_Bitcoin-apoie_o_projeto-F7931A?style=for-the-badge&logo=bitcoin&logoColor=white" alt="Apoie via Bitcoin">
  </a>
</p>

<p align="center">
  Gostou do projeto? Uma doação em Bitcoin, de qualquer valor, ajuda a manter o Quixote
  atacando moinhos de vento.
</p>

<p align="center"><code>bc1qtu6nvfjdcujpweazeq8w0et0vs7swmef75nurw</code></p>