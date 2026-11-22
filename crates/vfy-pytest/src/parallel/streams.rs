//! Replay of worker output streams in the parent process.

use super::{DIAGNOSTIC_STREAM_BYTES, OUTPUT_CHUNK_BYTES, io_error};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes};
use std::fmt::Write as _;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

pub(super) fn replay_worker_stream(
    py: Python<'_>,
    directory: &Path,
    prefix: &str,
    workers: usize,
) -> PyResult<()> {
    let mut previous_ends_with_newline = true;
    replay_worker_stream_range(
        py,
        directory,
        prefix,
        0..workers,
        &mut previous_ends_with_newline,
    )
}

pub(super) fn replay_worker_stream_range(
    py: Python<'_>,
    directory: &Path,
    prefix: &str,
    workers: std::ops::Range<usize>,
    previous_ends_with_newline: &mut bool,
) -> PyResult<()> {
    let stream = py.import("sys")?.getattr(prefix)?;
    let encoding = worker_stream_encoding(py, prefix)?;
    let decoder_class = py
        .import("codecs")?
        .call_method1("getincrementaldecoder", (encoding,))?;
    let mut buffer = vec![0; OUTPUT_CHUNK_BYTES];
    for index in workers {
        let mut file = File::open(directory.join(format!("{prefix}.{index}"))).map_err(io_error)?;
        let decoder = decoder_class.call1(("replace",))?;
        let mut first_chunk = true;
        loop {
            py.check_signals()?;
            let count = file.read(&mut buffer).map_err(io_error)?;
            let text = decoder
                .call_method1("decode", (PyBytes::new(py, &buffer[..count]), count == 0))?
                .extract::<String>()?;
            if !text.is_empty() {
                if first_chunk && !*previous_ends_with_newline {
                    stream.call_method1("write", ("\n",))?;
                }
                stream.call_method1("write", (&text,))?;
                *previous_ends_with_newline = text.ends_with('\n');
                first_chunk = false;
            }
            if count == 0 {
                break;
            }
        }
    }
    Ok(())
}

fn stream_encoding(stream: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    Ok(stream
        .getattr_opt("encoding")?
        .map(|encoding| encoding.extract::<Option<String>>())
        .transpose()?
        .flatten())
}

fn worker_stream_encoding(py: Python<'_>, prefix: &str) -> PyResult<String> {
    let sys = py.import("sys")?;
    // Workers inherit the original Python stream's encoding, independently
    // of coordinator capture streams and text-only output adapters.
    Ok(
        match stream_encoding(&sys.getattr(format!("__{prefix}__"))?)? {
            Some(encoding) => encoding,
            None => stream_encoding(&sys.getattr(prefix)?)?.unwrap_or_else(|| "utf-8".to_owned()),
        },
    )
}

pub(super) fn read_worker_tails(
    py: Python<'_>,
    directory: &Path,
    prefix: &str,
    workers: usize,
) -> PyResult<String> {
    let encoding = worker_stream_encoding(py, prefix)?;
    let per_worker = DIAGNOSTIC_STREAM_BYTES / workers.max(1);
    let mut output = String::new();
    for index in 0..workers {
        let mut file = File::open(directory.join(format!("{prefix}.{index}"))).map_err(io_error)?;
        let size = file.metadata().map_err(io_error)?.len();
        let start = size.saturating_sub(per_worker as u64);
        file.seek(SeekFrom::Start(start)).map_err(io_error)?;
        let mut bytes = Vec::with_capacity(per_worker);
        file.take(per_worker as u64)
            .read_to_end(&mut bytes)
            .map_err(io_error)?;
        if bytes.is_empty() {
            continue;
        }
        let _ = write!(output, "[worker {index}");
        if start != 0 {
            output.push_str("; earlier output omitted");
        }
        output.push_str("]\n");
        output.push_str(
            &PyBytes::new(py, &bytes)
                .call_method1("decode", (&encoding, "replace"))?
                .extract::<String>()?,
        );
        if !output.ends_with('\n') {
            output.push('\n');
        }
    }
    Ok(output)
}
