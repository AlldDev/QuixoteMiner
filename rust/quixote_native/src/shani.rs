//! Laço de nonce com SHA-NI escrito à mão, específico pro formato do header.
//!
//! Faz o mesmo que `busca_generica` (mesma saída, byte a byte), mas tira
//! proveito do que o `compress256` genérico não tem como saber:
//!
//! - **Rodadas 0 e 1 pré-calculadas**: no bloco 2 do header, `W0`/`W1`
//!   (bytes 64..72) não mudam dentro do lote, então o primeiro
//!   `sha256rnds2` sai do laço.
//! - **Saída antecipada**: no SHA-256, `h` depois de 64 rodadas é `f`
//!   depois de 62 (`h(t+2) = f(t)`). A palavra 7 do hash, que decide a
//!   rejeição rápida, fica pronta um `sha256rnds2` antes do fim; o último
//!   só roda pros raros nonces que passam.
//! - Palavras montadas direto em registrador, sem serializar o estado em
//!   bytes entre o primeiro e o segundo hash.
//!
//! Layout do SHA-NI: o estado vive em dois registradores, `ABEF` (A nos
//! bits 127..96, F nos 31..0) e `CDGH`; cada `sha256rnds2` avança 2 rodadas
//! usando as 2 palavras `W+K` das lanes baixas do terceiro operando.

use std::arch::x86_64::*;

use sha2::compress256;
use sha2::digest::generic_array::GenericArray;

use super::{IV, empurra_se_bate, palavra_alta_max};

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// Quantos nonces correm intercalados por iteração. Medido no Ryzen 5 1600
/// (2026-10-16): N=1 17,9 MH/s, N=2 15,7, N=3 16,9, N=4 17,9 — intercalar
/// não esconde latência nenhuma aqui e N=2/3 ainda perdem (spill de
/// registrador), então fica o mais simples.
const N: usize = 1;

pub fn disponivel() -> bool {
    is_x86_feature_detected!("sha")
        && is_x86_feature_detected!("sse4.1")
        && is_x86_feature_detected!("ssse3")
}

/// 4 palavras de `K` a partir de `4 * i`, lane j = `K[4i + j]`.
macro_rules! kv {
    ($i:expr) => {
        _mm_loadu_si128(K.as_ptr().add(4 * $i) as *const __m128i)
    };
}

/// Próximas 4 palavras do message schedule a partir das 16 anteriores.
macro_rules! sched {
    ($v0:expr, $v1:expr, $v2:expr, $v3:expr) => {
        _mm_sha256msg2_epu32(
            _mm_add_epi32(_mm_sha256msg1_epu32($v0, $v1), _mm_alignr_epi8($v3, $v2, 4)),
            $v3,
        )
    };
}

/// Estado em ordem H0..H7 (dois vetores, lanes H0..H3 e H4..H7) → ABEF/CDGH.
macro_rules! para_abef {
    ($h0123:expr, $h4567:expr) => {{
        let cdab = _mm_shuffle_epi32($h0123, 0xB1);
        let efgh = _mm_shuffle_epi32($h4567, 0x1B);
        (_mm_alignr_epi8(cdab, efgh, 8), _mm_blend_epi16(efgh, cdab, 0xF0))
    }};
}

/// ABEF/CDGH → H0..H3 e H4..H7.
macro_rules! de_abef {
    ($abef:expr, $cdgh:expr) => {{
        let feba = _mm_shuffle_epi32($abef, 0x1B);
        let dchg = _mm_shuffle_epi32($cdgh, 0xB1);
        (_mm_blend_epi16(feba, dchg, 0xF0), _mm_alignr_epi8(dchg, feba, 8))
    }};
}

/// Miner `count` nonces a partir de `start_nonce` — contrato idêntico a
/// `busca_generica`.
///
/// # Safety
/// Só pode ser chamada quando `disponivel()` devolve `true`.
#[target_feature(enable = "sha,sse2,ssse3,sse4.1")]
pub unsafe fn buscar(
    header_prefix: &[u8],
    start_nonce: u32,
    count: u32,
    target_pool: &[u8],
    target_rede: &[u8],
) -> Vec<(u32, Vec<u8>)> {
    let mut midstate = IV;
    compress256(&mut midstate, &[*GenericArray::from_slice(&header_prefix[..64])]);
    let be = |i: usize| u32::from_be_bytes(header_prefix[i..i + 4].try_into().unwrap());
    let (w0, w1, w2) = (be(64), be(68), be(72));
    let hi_max = palavra_alta_max(target_pool, target_rede);

    unsafe {
        let ld = |s: &[u32]| _mm_loadu_si128(s.as_ptr() as *const __m128i);
        let (abef_mid, cdgh_mid) = para_abef!(ld(&midstate[..4]), ld(&midstate[4..]));
        let (abef_iv, cdgh_iv) = para_abef!(ld(&IV[..4]), ld(&IV[4..]));

        // Rodadas 0 e 1 do bloco 2: só dependem de W0/W1, fixos no lote.
        let wk01 = _mm_add_epi32(_mm_set_epi32(0, 0, w1 as i32, w0 as i32), kv!(0));
        let cdgh_r2 = _mm_sha256rnds2_epu32(cdgh_mid, abef_mid, wk01);

        // Palavras constantes: padding do bloco 2 (80 bytes = 640 bits) e
        // do segundo hash (32 bytes = 256 bits).
        let pad80 = _mm_set_epi32(0, 0, 0, 0x8000_0000u32 as i32);
        let zero = _mm_setzero_si128();
        let len640 = _mm_set_epi32(640, 0, 0, 0);
        let len256 = _mm_set_epi32(256, 0, 0, 0);

        let mut found = Vec::new();
        let mut feito = 0u32;
        while count - feito >= N as u32 {
            let base = start_nonce.wrapping_add(feito);

            // ---- primeiro SHA-256, bloco 2 (o midstate cobre o bloco 1)
            let mut abef = [abef_mid; N];
            let mut cdgh = [cdgh_r2; N];
            let mut m = [[zero; 16]; N];
            for j in 0..N {
                let nonce = base.wrapping_add(j as u32);
                m[j][0] = _mm_set_epi32(nonce.swap_bytes() as i32, w2 as i32, w1 as i32, w0 as i32);
                m[j][1] = pad80;
                m[j][2] = zero;
                m[j][3] = len640;
            }
            for i in 0..16 {
                let k = kv!(i);
                for j in 0..N {
                    if i >= 4 {
                        m[j][i] = sched!(m[j][i - 4], m[j][i - 3], m[j][i - 2], m[j][i - 1]);
                    }
                    let wk = _mm_add_epi32(m[j][i], k);
                    if i > 0 {
                        cdgh[j] = _mm_sha256rnds2_epu32(cdgh[j], abef[j], wk);
                    }
                    abef[j] = _mm_sha256rnds2_epu32(abef[j], cdgh[j], _mm_shuffle_epi32(wk, 0x0E));
                }
            }

            // ---- segundo SHA-256: o hash do primeiro + padding, a partir do IV
            for j in 0..N {
                let (h0123, h4567) =
                    de_abef!(_mm_add_epi32(abef[j], abef_mid), _mm_add_epi32(cdgh[j], cdgh_mid));
                m[j][0] = h0123;
                m[j][1] = h4567;
                m[j][2] = pad80;
                m[j][3] = len256;
                abef[j] = abef_iv;
                cdgh[j] = cdgh_iv;
            }
            for i in 0..15 {
                let k = kv!(i);
                for j in 0..N {
                    if i >= 4 {
                        m[j][i] = sched!(m[j][i - 4], m[j][i - 3], m[j][i - 2], m[j][i - 1]);
                    }
                    let wk = _mm_add_epi32(m[j][i], k);
                    cdgh[j] = _mm_sha256rnds2_epu32(cdgh[j], abef[j], wk);
                    abef[j] = _mm_sha256rnds2_epu32(abef[j], cdgh[j], _mm_shuffle_epi32(wk, 0x0E));
                }
            }
            // Rodadas 60..63: depois das 60 e 61, F (lane 0 de ABEF) já é o
            // `h` final.
            let k15 = kv!(15);
            for j in 0..N {
                m[j][15] = sched!(m[j][11], m[j][12], m[j][13], m[j][14]);
                let wk = _mm_add_epi32(m[j][15], k15);
                let abef62 = _mm_sha256rnds2_epu32(cdgh[j], abef[j], wk);
                let h7 = (_mm_cvtsi128_si32(abef62) as u32).wrapping_add(IV[7]);
                // bytes 28..32 do hash = H7 em BE, lidos como u32 LE = swap_bytes.
                if h7.swap_bytes() > hi_max {
                    continue;
                }
                let abef64 = _mm_sha256rnds2_epu32(abef[j], abef62, _mm_shuffle_epi32(wk, 0x0E));
                let (h0123, h4567) = de_abef!(
                    _mm_add_epi32(abef64, abef_iv),
                    _mm_add_epi32(abef62, cdgh_iv)
                );
                let mut st2 = [0u32; 8];
                _mm_storeu_si128(st2.as_mut_ptr() as *mut __m128i, h0123);
                _mm_storeu_si128(st2.as_mut_ptr().add(4) as *mut __m128i, h4567);
                empurra_se_bate(
                    &mut found,
                    base.wrapping_add(j as u32),
                    &st2,
                    target_pool,
                    target_rede,
                );
            }
            feito += N as u32;
        }

        // Sobra quando `count` não é múltiplo de N: pelo caminho genérico, que
        // dá o mesmo resultado.
        if feito < count {
            found.extend(super::busca_generica(
                header_prefix,
                start_nonce.wrapping_add(feito),
                count - feito,
                target_pool,
                target_rede,
            ));
        }
        found
    }
}
