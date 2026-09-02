"""Leitura de `.env`, compartilhada por `daemon.py`, `cli.py` e `ui/dashboard.py`.

Cada variável exigida daqui levanta `SystemExit` com uma mensagem clara se
estiver ausente ou inválida — nunca cai num valor padrão silencioso. É o
mesmo tratamento que `BTC_ADDRESS` já recebia, generalizado: o `.env` é a
única fonte de cada um desses valores, sem um segundo default hardcoded
espalhado pelo código pra manter em sincronia.
"""

import pathlib

_BOOL_TRUE = {"true", "1", "sim"}
_BOOL_FALSE = {"false", "0", "não", "nao"}


def read_env(repo_root: pathlib.Path) -> dict[str, str]:
    """Parser mínimo de `KEY=VALUE`, um par por linha, `#` como comentário."""
    env_path = repo_root / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text().splitlines():
        raw_line = raw_line.strip()
        if not raw_line or raw_line.startswith("#") or "=" not in raw_line:
            continue
        key, _, value = raw_line.partition("=")
        values[key.strip()] = value.strip()
    return values


def require_str(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise SystemExit(f"defina {key} em .env")
    return value


def require_float(env: dict[str, str], key: str) -> float:
    raw = require_str(env, key)
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{key}={raw!r} em .env é inválido, esperado um número") from None


def require_int(env: dict[str, str], key: str) -> int:
    raw = require_str(env, key)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{key}={raw!r} em .env é inválido, esperado um inteiro") from None


def require_bool(env: dict[str, str], key: str) -> bool:
    raw = require_str(env, key).strip().lower()
    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False
    raise SystemExit(
        f"{key}={raw!r} em .env é inválido, esperado um de {sorted(_BOOL_TRUE | _BOOL_FALSE)}"
    )


def require_choice(env: dict[str, str], key: str, choices: tuple[str, ...]) -> str:
    raw = require_str(env, key)
    if raw not in choices:
        raise SystemExit(f"{key}={raw!r} em .env é inválido, esperado um de {choices}")
    return raw
