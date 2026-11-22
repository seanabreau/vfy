#[cfg(unix)]
use std::io::Write;

use criterion::Criterion;
#[cfg(unix)]
use criterion::{BenchmarkId, Throughput};
#[cfg(unix)]
use vfy_core::CaptureReader;

#[cfg(unix)]
pub(crate) fn benchmark_capture_reads(criterion: &mut Criterion) {
    let mut group = criterion.benchmark_group("native_capture_read");
    for size in [32 * 1024, 1024 * 1024, 8 * 1024 * 1024] {
        let expected = "x".repeat(size);
        let mut file = tempfile::tempfile().expect("capture benchmark file");
        file.write_all(expected.as_bytes())
            .expect("capture benchmark output");
        let mut reader = CaptureReader::default();
        let mut offset = 0;
        assert_eq!(
            reader.read(&file, &mut offset).expect("capture parity"),
            expected
        );
        assert_eq!(offset, u64::try_from(size).expect("capture size"));
        group.throughput(Throughput::Bytes(
            u64::try_from(size).expect("capture size"),
        ));
        group.bench_with_input(BenchmarkId::from_parameter(size), &file, |bencher, file| {
            bencher.iter(|| {
                let mut offset = 0;
                reader.read(file, &mut offset).expect("capture read")
            });
        });
    }
    group.finish();
}

#[cfg(not(unix))]
pub(crate) fn benchmark_capture_reads(_: &mut Criterion) {}
