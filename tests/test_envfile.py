"""Cobertura de envfile.py (invariante 'nada sem teste')."""

import pytest

from quixote.envfile import read_env, require_bool, require_choice, require_float, require_int


def _write_env(tmp_path, content: str):
    (tmp_path / ".env").write_text(content)
    return tmp_path


def test_read_env_ausente_devolve_dict_vazio(tmp_path):
    assert read_env(tmp_path) == {}


def test_read_env_ignora_comentarios_e_linhas_vazias(tmp_path):
    _write_env(
        tmp_path,
        "# comentário\n\nBTC_ADDRESS=bc1qexemplo\nPOOL_PORT=21496\n",
    )
    assert read_env(tmp_path) == {"BTC_ADDRESS": "bc1qexemplo", "POOL_PORT": "21496"}


def test_require_float_valido_e_invalido(tmp_path):
    env = read_env(_write_env(tmp_path, "CPU_TDP_WATTS=65.0\nBAD=abc\n"))
    assert require_float(env, "CPU_TDP_WATTS") == 65.0
    with pytest.raises(SystemExit):
        require_float(env, "BAD")


def test_require_float_ausente(tmp_path):
    env = read_env(_write_env(tmp_path, ""))
    with pytest.raises(SystemExit):
        require_float(env, "CPU_TDP_WATTS")


def test_require_int_valido_e_invalido(tmp_path):
    env = read_env(_write_env(tmp_path, "BATCH_SIZE=2000\nBAD=2.5\n"))
    assert require_int(env, "BATCH_SIZE") == 2000
    with pytest.raises(SystemExit):
        require_int(env, "BAD")


def test_require_bool_variacoes_aceitas(tmp_path):
    env = read_env(_write_env(tmp_path, "A=true\nB=1\nC=sim\nD=false\nE=0\nF=não\nG=talvez\n"))
    assert require_bool(env, "A") is True
    assert require_bool(env, "B") is True
    assert require_bool(env, "C") is True
    assert require_bool(env, "D") is False
    assert require_bool(env, "E") is False
    assert require_bool(env, "F") is False
    with pytest.raises(SystemExit):
        require_bool(env, "G")


def test_require_choice_valido_e_invalido(tmp_path):
    env = read_env(_write_env(tmp_path, "MODO_TESTE=manual\nBAD=turbo\n"))
    assert require_choice(env, "MODO_TESTE", ("auto", "manual")) == "manual"
    with pytest.raises(SystemExit):
        require_choice(env, "BAD", ("auto", "manual"))
