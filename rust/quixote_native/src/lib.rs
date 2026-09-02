//! Laço quente de mineração do Quixote, em Rust.
//!
//! Único módulo nativo do projeto — substitui de vez o laço Python que
//! existia em `quixote/core/hasher.py` (não é um fallback opcional;
//! decisão de 2026-09-02). Fronteira PyO3 em lote (um
//! `search_nonces` por lote de nonces, não um por nonce): a granularidade
//! grossa evita que o overhead de cada chamada Python→Rust coma o ganho de
//! velocidade do laço.
//!
//! Convenção de bytes idêntica ao resto do projeto
//! (`quixote/core/hashing.py`): hashes e targets são little-endian
//! "corridos", a mesma ordem que sai direto de `sha256d`, sem reversão.

use pyo3::prelude::*;
use sha2::{Digest, Sha256};

/// sha256(sha256(data)) — mesma primitiva de `core.hashing.sha256d`.
fn sha256d(data: &[u8]) -> [u8; 32] {
    let first = Sha256::digest(data);
    let second = Sha256::digest(first);
    second.into()
}

/// Compara `a` (hash, 32 bytes little-endian) < `b` (target, mesma
/// convenção) como inteiros, sem crate de bignum: num número
/// little-endian o byte mais significativo é o último (índice 31), então
/// comparamos de trás pra frente e paramos na primeira diferença.
fn less_than_le(a: &[u8; 32], b: &[u8]) -> bool {
    for i in (0..32).rev() {
        if a[i] != b[i] {
            return a[i] < b[i];
        }
    }
    false
}

/// Minera um lote de `count` nonces a partir de `start_nonce`. Devolve
/// `(nonce, header_hash)` de cada acerto contra `target_pool` **ou**
/// `target_rede` (o lado Python decide qual dos dois bateu e o que fazer
/// a respeito — bloco de verdade vs. share da pool — sem duplicar essa
/// lógica aqui).
#[pyfunction]
fn search_nonces(
    header_prefix: &[u8],
    start_nonce: u32,
    count: u32,
    target_pool: &[u8],
    target_rede: &[u8],
) -> PyResult<Vec<(u32, Vec<u8>)>> {
    if header_prefix.len() != 76 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "header_prefix precisa ter 76 bytes (os 80 do header menos o nonce)",
        ));
    }
    if target_pool.len() != 32 || target_rede.len() != 32 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "target_pool/target_rede precisam ter 32 bytes little-endian",
        ));
    }

    let mut buffer = [0u8; 80];
    buffer[..76].copy_from_slice(header_prefix);

    let mut found = Vec::new();
    for offset in 0..count {
        let nonce = start_nonce.wrapping_add(offset);
        buffer[76..80].copy_from_slice(&nonce.to_le_bytes());
        let hash = sha256d(&buffer);
        if less_than_le(&hash, target_pool) || less_than_le(&hash, target_rede) {
            found.push((nonce, hash.to_vec()));
        }
    }
    Ok(found)
}

/// Exposta só pra conferência de corretude do lado Python (ver
/// `tests/test_hasher_native.py`): mesma primitiva de `search_nonces`,
/// sem laço, comparada byte a byte contra `core.hashing.sha256d`.
#[pyfunction]
fn sha256d_py(data: &[u8]) -> PyResult<Vec<u8>> {
    Ok(sha256d(data).to_vec())
}

#[pymodule]
fn quixote_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(search_nonces, m)?)?;
    m.add_function(wrap_pyfunction!(sha256d_py, m)?)?;
    Ok(())
}
