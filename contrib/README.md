# Serviço `quixote.service`

Configuração e instalação do daemon como serviço de usuário do systemd. Só
o necessário pra instalar e rodar — explicação didática do protocolo e do
`--explain` ficam no [README principal](../README.md).

## 1. Pré-requisitos

- `BTC_ADDRESS` configurado (passo 2).
- Uma `.venv/` local, com o pacote instalado — ela não vai pro git, precisa
  existir na máquina onde o serviço vai rodar. Nesta máquina o Python do
  sistema **não tem `pip`** (`python3 -m pip` → `No module named pip`), mas
  o módulo `venv` cria sua própria `pip` isolada, então não precisa
  instalar nada no sistema:

```
python3 -m venv .venv
.venv/bin/pip install -e .
```

Esse comando é o que gera o executável `quixote` dentro da `.venv`.
Confirme que funcionou:

```
.venv/bin/quixote --help
```

**Não copie a pasta do projeto pra `~/.local/bin/quixote`** — isso cria um
*diretório* lá, não um executável, e quebra o comando (`bash:
.../quixote: É um diretório`). É o `pip install -e .` dentro da `.venv`
que gera o script de verdade.

### 1.1 Laço de mineração (Rust, obrigatório)

O laço quente de mineração (`quixote/core/hasher.py`) roda em Rust
(`quixote_native`, `rust/quixote_native/`) — não em Python puro, não é
opcional. Sem o módulo compilado, `import quixote.core.hasher` falha
(`ModuleNotFoundError: quixote_native`), e o daemon nem sobe.

Instale o toolchain de sistema uma vez:

```
sudo dnf install rust cargo maturin
```

Depois, com a `.venv` já criada (passo anterior), compile o módulo — o
comando precisa rodar **de dentro de `rust/quixote_native/`** (é onde
está o `Cargo.toml` do crate):

```
cd rust/quixote_native && ../../.venv/bin/maturin develop --release
```

Repita esse comando sempre que o código em `rust/quixote_native/` mudar —
`pip install -e .` não recompila o Rust sozinho.

**Por quê**: medido nesta máquina (Zen 1, sem SHA-NI), o mesmo laço de
mineração em Rust sustenta ~6,5 milhões de hashes/s contra ~700 mil/s do
Python puro — sustentar os 350 KH/s travados custa ~5% de um núcleo em vez
de ~46%, e o consumo marginal medido cai de ~2,4 W pra uma fração de watt.
Decisão de manter só essa implementação (sem caminho Python de fallback),
tomada em 2026-09-02.

## 2. Configuração (`.env`)

Copie o exemplo na raiz do projeto e preencha o seu endereço:

```
cp .example.env .env
```

Variáveis:

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `BTC_ADDRESS` | sim | — | Seu endereço BTC. Usado como usuário no pool, no formato `endereco.desktop`. |
| `TARGET_HASHRATE` | não | `350000` | Hashrate-alvo em H/s. Só muda com edição explícita do `.env` — não recarrega sozinho, precisa reiniciar o serviço (passo 5). |
| `TARIFF_BRL_PER_KWH` | não | — | Tarifa de energia, pro painel calcular custo em reais. Sem ela, custo fica indisponível. |

As demais variáveis (`WORKER_NAME`, `BATCH_SIZE`, `POOL_HOST`, `POOL_PORT`, `SUGGEST_DIFFICULTY`,
`RECONNECT_MAX_BACKOFF`, `CPU_TDP_WATTS`, `POWER_STRATEGY`, `REFRESH_HZ`, `SHOW_PROGRESS_JOKE`,
`LOG_LEVEL`, `LOG_FILE`, `LOG_MAX_SIZE_MB`) são obrigatórias, mas o `.example.env` já vem com elas
preenchidas — o passo acima (`cp .example.env .env`) já resolve todas, só `BTC_ADDRESS` precisa de
edição pra sair do padrão. Sem alguma delas, o daemon/painel recusam subir com uma mensagem clara
dizendo qual falta, em vez de assumir um valor por conta própria.

`.env` fica fora do versionamento (veja `.gitignore`) — nunca commite o seu.

## 3. Instalar o unit

`contrib/quixote.service` traz `ExecStart` com o placeholder `__QUIXOTE_DIR__` — o comando abaixo substitui pelo caminho absoluto do checkout atual (rode a partir da raiz do repositório):

```
mkdir -p ~/.config/systemd/user
sed "s#__QUIXOTE_DIR__#$(pwd)#" contrib/quixote.service > ~/.config/systemd/user/quixote.service
systemctl --user daemon-reload
```

## 4. Habilitar e iniciar

```
systemctl --user enable --now quixote.service
loginctl enable-linger $USER
```

O `enable-linger` é o que mantém o serviço rodando mesmo sem sessão gráfica
aberta — sem ele, o daemon morre ao deslogar.

## 5. Operar

| Ação | Comando |
|---|---|
| Ver status | `systemctl --user status quixote.service` |
| Ver logs | `journalctl --user -u quixote.service -f` |
| Reiniciar (aplica mudança no `.env`) | `systemctl --user restart quixote.service` |
| Parar | `systemctl --user stop quixote.service` |
| Desabilitar | `systemctl --user disable quixote.service` |

## 6. Verificação

1. `systemctl --user status quixote.service` deve mostrar `active (running)`.
2. `journalctl --user -u quixote.service -f` deve mostrar a conexão com o
   pool, jobs chegando e o hashrate se estabilizando perto do
   `TARGET_HASHRATE` configurado.
3. Confira `https://web.public-pool.io/#/app/SEU_ENDERECO` — o worker
   `desktop` deve aparecer lá.

## 7. Leitura de energia real (RAPL, opcional)

Sem isso o daemon ainda funciona, só cai pra estratégia de watts estimados
(`ESTIMADO` em vez de `RAPL`/`PROPORCIONAL` no log) — o kernel restringe
`energy_uj` a root desde a mitigação da CVE-2020-8694. `contrib/99-rapl-read.rules`
reabre a leitura pra qualquer usuário local; pense duas vezes antes de
instalar em máquina compartilhada ou multiusuário.

```
sudo cp contrib/99-rapl-read.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=powercap
```

Teste: `cat /sys/class/powercap/intel-rapl:0/energy_uj` deve mostrar um
número sem pedir senha.

## 8. Log em arquivo (opcional)

`LOG_FILE` (`.example.env`) já vem apontando pra `/var/log/quixote/quixote.log`,
mas o serviço roda como usuário comum, não root — sem o diretório criado com
o dono certo, o daemon sobe normal mesmo assim, só sem log em arquivo (loga
um aviso explicando). Pra habilitar de verdade, uma vez só:

```
sudo install -d -o "$USER" -g "$USER" -m 0755 /var/log/quixote
```

Sem isso (ou se preferir outro lugar), edite `LOG_FILE` no `.env` — qualquer
caminho onde o usuário tenha permissão de escrita funciona, por exemplo
`~/.local/share/quixote/quixote.log`. A rotação (`LOG_MAX_SIZE_MB`) vale
pra qualquer um dos dois. No painel (`quixote top`), a tecla `l` abre esse
arquivo no seu `$PAGER` (`less` por padrão).

## O que o `.service` já limita sozinho

**`CPUQuota` é rede de segurança, não o throttle.** Quem mantém o
hashrate no alvo é `core/hasher.py` (`target_hashrate`, mede o tempo de
cada lote e dorme o que sobrar) — isso sozinho já é suficiente em
operação normal. `CPUQuota=65%` só existe pra cobrir o cenário de um bug
nesse cálculo: se o throttle falhar e o hasher tentar rodar sem freio, o
`CPUQuota` garante que o processo não passa de 65% de um núcleo, em vez
de tomar a máquina inteira. `contrib/quixote.service` também vem com
`Nice=19`, `MemoryMax=200M` e `OOMScoreAdjust=800` — mesma categoria de
contenção, não mecanismos de operação normal.

Se o painel mostrar hashrate travado bem abaixo do alvo **com uso de CPU
baixo**, é sinal de que essa rede de segurança está ativa e cortando o
throttle antes da hora (foi exatamente o que aconteceu antes de
2026-09-02, com `CPUQuota=8%`) — o primeiro lugar
pra checar é `systemctl --user status quixote` e `cat
/sys/fs/cgroup/.../quixote.service/cpu.max`.

O valor de `CPUQuota` depende do hardware **e** de qual implementação do
laço de mineração está rodando. Com o Python puro (antes de 2026-09-02),
sustentar 350 KH/s nesta máquina custava ~44-52% de um núcleo — os 65%
davam folga real. Com o laço em Rust (`quixote_native`, item 1.1), o mesmo
alvo custa bem menos (~5-16% de um núcleo, dependendo de `BATCH_SIZE`) —
os 65% continuam funcionando, só que agora com muito mais folga do que o
necessário. Não reduzido nesta migração (fora do escopo decidido); fica
como ajuste possível pra quem quiser uma quota mais apertada.
