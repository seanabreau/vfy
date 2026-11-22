//! Unix captured-output reads that preserve a redirected writer's file position.

use std::fs::File;
use std::io::{self, Seek, SeekFrom};
use std::os::unix::fs::FileExt;

const READ_BUFFER_SIZE: usize = 64 * 1024;

/// Reusable scratch space for stdout and stderr snapshots in one session.
pub struct CaptureReader {
    buffer: Vec<u8>,
}

impl Default for CaptureReader {
    fn default() -> Self {
        Self {
            buffer: vec![0; READ_BUFFER_SIZE],
        }
    }
}

impl CaptureReader {
    /// Read new output without moving the position shared with redirected writers.
    ///
    /// Invalid UTF-8 is replaced after joining all chunks, so a multibyte
    /// character split across reads is decoded once. Valid UTF-8 reuses the
    /// collected byte allocation as the returned string.
    ///
    /// # Errors
    /// Returns a read error and leaves `offset` unchanged, so a retry reads the
    /// same bytes again instead of losing the chunks consumed before the error.
    pub fn read(&mut self, file: &File, offset: &mut u64) -> io::Result<String> {
        self.read_with(offset, |buffer, position| file.read_at(buffer, position))
    }

    fn read_with(
        &mut self,
        offset: &mut u64,
        mut read_at: impl FnMut(&mut [u8], u64) -> io::Result<usize>,
    ) -> io::Result<String> {
        let mut bytes = Vec::new();
        let mut position = *offset;
        loop {
            match read_at(&mut self.buffer, position) {
                Ok(0) => break,
                Ok(count) => {
                    bytes.extend_from_slice(&self.buffer[..count]);
                    position += count as u64;
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(error) => return Err(error),
            }
        }
        *offset = position;
        Ok(match String::from_utf8(bytes) {
            Ok(text) => text,
            Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
        })
    }

    /// Read and clear a phase's output, resetting the redirected writer position.
    ///
    /// Like pytest's `FDCapture.snap`, this truncates after the read: bytes a
    /// still-running subprocess writes between the two calls are dropped, and the
    /// capture file stays bounded by one phase's output.
    ///
    /// # Errors
    /// Returns an error when reading, truncation, or seeking fails. A failed
    /// read or truncation leaves the offset unchanged.
    pub fn snapshot(&mut self, mut file: &File, offset: &mut u64) -> io::Result<String> {
        let mut position = *offset;
        let output = self.read(file, &mut position)?;
        file.set_len(0)?;
        *offset = 0;
        // Seeking resets the shared position of inherited subprocess
        // descriptors and the original redirected stream too.
        file.seek(SeekFrom::Start(0))?;
        Ok(output)
    }
}

#[cfg(test)]
mod tests;
