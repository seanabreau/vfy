//! Packed line sets (`numbits` blobs) and sparse per-file evidence tables.

use std::error::Error;
use std::fmt;
use std::hash::{Hash, Hasher};
use std::ops::Range;
use std::sync::Arc as Shared;

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

type Arc = (i32, i32);

#[cfg(test)]
mod tests;

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct NumbitsRangeError;

impl fmt::Display for NumbitsRangeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "line_bits blob exceeds the supported line range 0..={}",
            i32::MAX
        )
    }
}

impl Error for NumbitsRangeError {}

fn validate_numbits_len(byte_len: usize) -> Result<(), NumbitsRangeError> {
    let Some(last_byte) = byte_len.checked_sub(1) else {
        return Ok(());
    };
    let last_line = last_byte.saturating_mul(8).saturating_add(7);
    i32::try_from(last_line).map_err(|_| NumbitsRangeError)?;
    Ok(())
}

fn sorted_chunk_lines(chunks: &HashMap<i32, u64>) -> Vec<i32> {
    let mut lines = Vec::with_capacity(
        chunks
            .values()
            .map(|chunk| chunk.count_ones() as usize)
            .sum(),
    );
    for (&chunk, &bits) in chunks {
        let mut remaining = bits;
        while remaining != 0 {
            let bit = remaining.trailing_zeros();
            lines.push(chunk * 64 + i32::try_from(bit).unwrap_or_default());
            remaining &= remaining - 1;
        }
    }
    lines.sort_unstable();
    lines
}

/// Shares identical line sets between finished contexts.
///
/// Parametrized tests often execute the same lines of a file, so one
/// allocation can serve many contexts. Shared sets are copied on write.
#[derive(Default)]
pub(crate) struct LineSetTable {
    sets: HashMap<u64, Vec<Shared<HashMap<i32, u64>>>>,
}

impl LineSetTable {
    /// Replace `lines` with an equal set already in the table, or add it.
    pub(crate) fn share(&mut self, lines: &mut SparseLines) {
        let candidates = self.sets.entry(Self::fingerprint(lines)).or_default();
        if let Some(existing) = candidates.iter().find(|candidate| {
            Shared::ptr_eq(candidate, &lines.chunks) || ***candidate == *lines.chunks
        }) {
            lines.chunks = Shared::clone(existing);
            return;
        }
        Shared::make_mut(&mut lines.chunks).shrink_to_fit();
        candidates.push(Shared::clone(&lines.chunks));
    }

    fn fingerprint(lines: &SparseLines) -> u64 {
        lines
            .chunks
            .iter()
            .fold(0_u64, |fingerprint, (&chunk, &bits)| {
                let mut hasher = rustc_hash::FxHasher::default();
                (chunk, bits).hash(&mut hasher);
                fingerprint.wrapping_add(hasher.finish())
            })
    }

    pub(crate) fn clear(&mut self) {
        self.sets.clear();
    }
}

/// Decodes stored `numbits` blobs, sharing one allocation per distinct set.
///
/// Trailing zero bytes do not change a set, so the leading zero-word count and
/// the remaining bytes through the last nonzero byte identify it exactly. A
/// repeated blob reuses the earlier allocation without being decoded again.
#[derive(Default)]
pub(crate) struct NumbitsTable {
    first: HashMap<u64, usize>,
    sets: Vec<DecodedNumbits>,
    bytes: Vec<u8>,
}

struct DecodedNumbits {
    leading_zeros: usize,
    bytes: Range<usize>,
    chunks: Shared<HashMap<i32, u64>>,
    next: Option<usize>,
}

impl NumbitsTable {
    // Inlining this lookup into the loader's row closure slows unique-set reads
    // in the extension's fat-LTO release build.
    #[inline(never)]
    pub(crate) fn decode(&mut self, numbits: &[u8]) -> Result<SparseLines, NumbitsRangeError> {
        validate_numbits_len(numbits.len())?;
        let used = numbits
            .iter()
            .rposition(|&byte| byte != 0)
            .map_or(0, |last| last + 1);
        let trimmed = &numbits[..used];
        let leading_words = trimmed
            .as_chunks::<8>()
            .0
            .iter()
            .take_while(|word| u64::from_ne_bytes(**word) == 0)
            .count();
        let leading_zeros = 8 * leading_words;
        let key = &trimmed[leading_zeros..];
        let fingerprint = Self::fingerprint(leading_zeros, key);
        let head = self.first.get(&fingerprint).copied();
        let mut candidate = head;
        while let Some(index) = candidate {
            let set = &self.sets[index];
            if set.leading_zeros == leading_zeros && self.bytes[set.bytes.clone()] == *key {
                return Ok(SparseLines {
                    chunks: Shared::clone(&set.chunks),
                });
            }
            candidate = set.next;
        }
        let lines = SparseLines::from_words(key, leading_words)?;
        let start = self.bytes.len();
        self.bytes.extend_from_slice(key);
        self.first.insert(fingerprint, self.sets.len());
        self.sets.push(DecodedNumbits {
            leading_zeros,
            bytes: start..self.bytes.len(),
            chunks: Shared::clone(&lines.chunks),
            next: head,
        });
        Ok(lines)
    }

    /// Forget every set while keeping the allocated capacity.
    pub(crate) fn clear(&mut self) {
        self.first.clear();
        self.sets.clear();
        self.bytes.clear();
    }

    fn fingerprint(leading_zeros: usize, key: &[u8]) -> u64 {
        let mut hasher = rustc_hash::FxHasher::default();
        hasher.write_usize(leading_zeros);
        hasher.write(key);
        hasher.finish()
    }
}

/// Sparse executed-line evidence used by collection and coverage data.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct SparseLines {
    chunks: Shared<HashMap<i32, u64>>,
}

impl SparseLines {
    #[cfg(test)]
    pub(crate) fn from_numbits(numbits: &[u8]) -> Result<Self, NumbitsRangeError> {
        validate_numbits_len(numbits.len())?;
        Self::from_words(numbits, 0)
    }

    /// Decode little-endian 64-line words whose first word is `first_word`.
    fn from_words(numbits: &[u8], first_word: usize) -> Result<Self, NumbitsRangeError> {
        let (words, rest) = numbits.as_chunks::<8>();
        let mut last = [0; 8];
        last[..rest.len()].copy_from_slice(rest);
        let mut chunks = HashMap::default();
        for (offset, word) in words.iter().chain([&last]).enumerate() {
            let bits = u64::from_le_bytes(*word);
            if bits == 0 {
                continue;
            }
            let chunk = i32::try_from(first_word + offset).map_err(|_| NumbitsRangeError)?;
            chunks.insert(chunk, bits);
        }
        Ok(Self {
            chunks: Shared::new(chunks),
        })
    }

    pub(crate) fn to_numbits(&self) -> Vec<u8> {
        let Some((&last_chunk, &last_bits)) = self
            .chunks
            .iter()
            .filter(|(chunk, bits)| **chunk >= 0 && **bits != 0)
            .max_by_key(|(chunk, _)| **chunk)
        else {
            return Vec::new();
        };
        let last_byte = last_chunk.cast_unsigned() as usize * 8 + last_bits.ilog2() as usize / 8;
        let mut numbits = vec![0; last_byte + 1];
        for (&chunk, &bits) in self.chunks.iter().filter(|(chunk, _)| **chunk >= 0) {
            let start = chunk.cast_unsigned() as usize * 8;
            let end = (start + 8).min(numbits.len());
            if start < end {
                numbits[start..end].copy_from_slice(&bits.to_le_bytes()[..end - start]);
            }
        }
        numbits
    }

    pub(crate) fn insert(&mut self, line: i32) {
        let chunk = line.div_euclid(64);
        let bit = line.rem_euclid(64).cast_unsigned();
        *Shared::make_mut(&mut self.chunks).entry(chunk).or_default() |= 1_u64 << bit;
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = i32> + '_ {
        self.chunks.iter().flat_map(|(&chunk, &bits)| {
            let mut remaining = bits;
            std::iter::from_fn(move || {
                if remaining == 0 {
                    return None;
                }
                let bit = remaining.trailing_zeros();
                remaining &= remaining - 1;
                Some(chunk * 64 + i32::try_from(bit).unwrap_or_default())
            })
        })
    }

    pub(crate) fn min_line(&self) -> Option<i32> {
        self.chunks
            .iter()
            .filter(|(_, bits)| **bits != 0)
            .map(|(&chunk, &bits)| {
                chunk * 64 + i32::try_from(bits.trailing_zeros()).unwrap_or_default()
            })
            .min()
    }

    pub(crate) fn contains(&self, line: i32) -> bool {
        let chunk = line.div_euclid(64);
        let bit = line.rem_euclid(64).cast_unsigned();
        self.chunks
            .get(&chunk)
            .is_some_and(|bits| bits & (1_u64 << bit) != 0)
    }

    pub(crate) fn extend_set(&mut self, other: &Self) {
        let mut incoming = other.chunks.iter();
        let first = if Shared::strong_count(&self.chunks) > 1 {
            if Shared::ptr_eq(&self.chunks, &other.chunks) {
                return;
            }
            incoming.find(|(chunk, bits)| {
                self.chunks
                    .get(*chunk)
                    .is_none_or(|current| *current & **bits != **bits)
            })
        } else {
            incoming.next()
        };
        let Some((&chunk, &bits)) = first else {
            return;
        };
        let chunks = Shared::make_mut(&mut self.chunks);
        *chunks.entry(chunk).or_default() |= bits;
        for (&chunk, &bits) in incoming {
            *chunks.entry(chunk).or_default() |= bits;
        }
    }

    pub(crate) fn sorted(&self) -> Vec<i32> {
        sorted_chunk_lines(&self.chunks)
    }

    #[cfg(test)]
    pub(crate) fn shares_allocation(&self, other: &Self) -> bool {
        Shared::ptr_eq(&self.chunks, &other.chunks)
    }
}

impl Extend<i32> for SparseLines {
    fn extend<T: IntoIterator<Item = i32>>(&mut self, iter: T) {
        for line in iter {
            self.insert(line);
        }
    }
}

/// Compact signed arc pairs with copy-on-write data sharing.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct SparseArcs {
    values: Shared<HashSet<u64>>,
}

impl SparseArcs {
    fn encode((start, end): Arc) -> u64 {
        (u64::from(start.cast_unsigned()) << 32) | u64::from(end.cast_unsigned())
    }

    fn decode(value: u64) -> Arc {
        let start = u32::try_from(value >> 32).unwrap_or_default().cast_signed();
        let end = u32::try_from(value & u64::from(u32::MAX))
            .unwrap_or_default()
            .cast_signed();
        (start, end)
    }

    pub(crate) fn insert(&mut self, arc: Arc) {
        Shared::make_mut(&mut self.values).insert(Self::encode(arc));
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = Arc> + '_ {
        self.values.iter().copied().map(Self::decode)
    }

    pub(crate) fn extend_set(&mut self, other: &Self) {
        let mut incoming = other.values.iter().copied();
        let first = if Shared::strong_count(&self.values) > 1 {
            if Shared::ptr_eq(&self.values, &other.values) {
                return;
            }
            incoming.find(|arc| !self.values.contains(arc))
        } else {
            incoming.next()
        };
        let Some(first) = first else {
            return;
        };
        let values = Shared::make_mut(&mut self.values);
        values.insert(first);
        values.extend(incoming);
    }

    pub(crate) fn sorted(&self) -> Vec<Arc> {
        let mut arcs: Vec<_> = self.values.iter().copied().map(Self::decode).collect();
        arcs.sort_unstable();
        arcs
    }
}

impl Extend<Arc> for SparseArcs {
    fn extend<T: IntoIterator<Item = Arc>>(&mut self, iter: T) {
        Shared::make_mut(&mut self.values).extend(iter.into_iter().map(Self::encode));
    }
}
