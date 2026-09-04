"""Parsing de argumentos: `daemon` (minerador) e `top` (painel ao vivo)."""

import argparse
import logging
import logging.handlers
import pathlib

from quixote import envfile

logger = logging.getLogger(__name__)

TRACE = 5
"""Nível abaixo de DEBUG (`logging` da stdlib não tem um pronto): montagem
do header a cada job, passos do merkle, primeiro hash de cada lote."""

_LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOG_BACKUP_COUNT = 3
"""Quantos arquivos de log rotacionados manter, além do atual. Não
especificado por nenhuma referência do projeto — só um padrão razoável."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quixote")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon_parser = subparsers.add_parser(
        "daemon", help="minerador em primeiro plano ou como serviço"
    )
    daemon_parser.add_argument(
        "--log-level",
        default=None,
        choices=_LOG_LEVELS,
        help="sobrescreve LOG_LEVEL do .env pra esta execução",
    )
    daemon_parser.add_argument(
        "--no-detach",
        action="store_true",
        help=(
            "fica em primeiro plano, sem fork/daemonização própria — o "
            "systemd, com Type=simple, já trata o processo em primeiro "
            "plano como o serviço em si"
        ),
    )
    daemon_parser.add_argument(
        "--explain",
        action="store_true",
        help=(
            "antes de minerar, imprime uma explicação passo a passo do "
            "primeiro job recebido: cada campo do header, cada etapa do "
            "merkle, o target expandido e o cálculo do throttle"
        ),
    )

    top_parser = subparsers.add_parser("top", help="painel ao vivo, lê o daemon via IPC")
    top_parser.add_argument(
        "--log-level",
        default=None,
        choices=_LOG_LEVELS,
        help="sobrescreve LOG_LEVEL do .env pra esta execução",
    )
    return parser


def _setup_logging(log_level_name: str, env: dict[str, str], is_daemon: bool) -> None:
    level = TRACE if log_level_name == "TRACE" else getattr(logging, log_level_name)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=level, format=fmt, handlers=[logging.StreamHandler()])

    log_file = env.get("LOG_FILE") if is_daemon else None
    if not log_file:
        return

    max_size_mb = envfile.require_int(env, "LOG_MAX_SIZE_MB")
    path = pathlib.Path(log_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_size_mb * 1024 * 1024, backupCount=_LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(logging.Formatter(fmt))
    except OSError as exc:
        # LOG_FILE em /var/log/ exige o diretório já criado com o dono certo
        # (contrib/README.md documenta o `sudo install -d`) — passo manual,
        # uma vez só, degrada gracioso se não tiver sido feito ainda.
        logger.warning("não foi possível abrir LOG_FILE=%s (%s) — sem log em arquivo", path, exc)
        return

    logging.getLogger().addHandler(file_handler)


def main(argv: list[str] | None = None) -> None:
    logging.addLevelName(TRACE, "TRACE")

    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    env = envfile.read_env(repo_root)
    log_level_name = args.log_level or envfile.require_choice(env, "LOG_LEVEL", _LOG_LEVELS)
    _setup_logging(log_level_name, env, is_daemon=args.command == "daemon")

    if args.command == "daemon":
        from quixote.daemon import run

        run(explain=args.explain)
    elif args.command == "top":
        from quixote.ui.dashboard import run as run_dashboard

        run_dashboard()
