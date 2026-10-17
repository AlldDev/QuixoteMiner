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

#[cfg(target_arch = "x86_64")]
mod shani;

use pyo3::prelude::*;
use sha2::digest::generic_array::GenericArray;
use sha2::{Digest, Sha256, compress256};

/// Estado inicial do SHA-256 (H0..H7, FIPS 180-4).
const IV: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];

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
///
/// Os primeiros 64 bytes do header não mudam dentro do lote, então a
/// primeira compressão SHA-256 (o "midstate") é feita uma vez só, fora do
/// laço; cada nonce custa 2 compressões em vez das 3 de `sha256d`, com os
/// paddings já pré-montados nos blocos fixos.
#[pyfunction]
fn search_nonces(
    py: Python<'_>,
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

    // Solta o GIL durante o laço quente: só mexe em bytes/buffer local, não
    // em objeto Python nenhum, então outras threads (Stratum, IPC) podem
    // rodar de verdade enquanto este lote hasheia, em vez de ficarem
    // congeladas esperando o GIL voltar a cada lote.
    let found = py.detach(|| {
        #[cfg(target_arch = "x86_64")]
        if shani::disponivel() {
            // SAFETY: as extensões que `buscar` habilita foram conferidas em runtime.
            return unsafe {
                shani::buscar(header_prefix, start_nonce, count, target_pool, target_rede)
            };
        }
        busca_generica(header_prefix, start_nonce, count, target_pool, target_rede)
    });
    Ok(found)
}

/// Maior palavra alta (bytes 28..32, little-endian) entre os dois targets:
/// um hash cuja palavra mais significativa passa disso não bate em nenhum
/// dos dois, sem precisar serializar nem comparar os 32 bytes.
fn palavra_alta_max(target_pool: &[u8], target_rede: &[u8]) -> u32 {
    let palavra_alta = |t: &[u8]| u32::from_le_bytes(t[28..32].try_into().unwrap());
    palavra_alta(target_pool).max(palavra_alta(target_rede))
}

/// Caminho portável (qualquer CPU), via `compress256` do crate `sha2` —
/// que por sua vez já usa SHA-NI quando existe, mas sem o intercalamento e
/// os atalhos de `shani::buscar`. Também é a referência de corretude dele.
fn busca_generica(
    header_prefix: &[u8],
    start_nonce: u32,
    count: u32,
    target_pool: &[u8],
    target_rede: &[u8],
) -> Vec<(u32, Vec<u8>)> {
    let mut midstate = IV;
    compress256(&mut midstate, &[*GenericArray::from_slice(&header_prefix[..64])]);

    // Bloco 2 do header: 12 bytes finais do prefixo + nonce + padding
    // (0x80, zeros, comprimento de 640 bits em big-endian).
    let mut bloco2 = [0u8; 64];
    bloco2[..12].copy_from_slice(&header_prefix[64..76]);
    bloco2[16] = 0x80;
    bloco2[56..].copy_from_slice(&640u64.to_be_bytes());

    // Bloco único do segundo hash: 32 bytes do primeiro + padding de 256 bits.
    let mut bloco_h2 = [0u8; 64];
    bloco_h2[32] = 0x80;
    bloco_h2[56..].copy_from_slice(&256u64.to_be_bytes());

    let hi_max = palavra_alta_max(target_pool, target_rede);

    let mut found = Vec::new();
    for offset in 0..count {
        let nonce = start_nonce.wrapping_add(offset);
        bloco2[12..16].copy_from_slice(&nonce.to_le_bytes());
        let mut st = midstate;
        compress256(&mut st, &[*GenericArray::from_slice(&bloco2)]);

        for (i, w) in st.iter().enumerate() {
            bloco_h2[i * 4..i * 4 + 4].copy_from_slice(&w.to_be_bytes());
        }
        let mut st2 = IV;
        compress256(&mut st2, &[*GenericArray::from_slice(&bloco_h2)]);

        // bytes 28..32 do hash = st2[7] em BE, lidos como u32 LE = swap_bytes.
        if st2[7].swap_bytes() > hi_max {
            continue;
        }
        empurra_se_bate(&mut found, nonce, &st2, target_pool, target_rede);
    }
    found
}

/// Serializa o estado final do segundo SHA-256 (o hash, na ordem que sai de
/// `sha256d`) e guarda `(nonce, hash)` se ele bater em algum dos targets.
fn empurra_se_bate(
    found: &mut Vec<(u32, Vec<u8>)>,
    nonce: u32,
    st2: &[u32; 8],
    target_pool: &[u8],
    target_rede: &[u8],
) {
    let mut hash = [0u8; 32];
    for (i, w) in st2.iter().enumerate() {
        hash[i * 4..i * 4 + 4].copy_from_slice(&w.to_be_bytes());
    }
    if less_than_le(&hash, target_pool) || less_than_le(&hash, target_rede) {
        found.push((nonce, hash.to_vec()));
    }
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
