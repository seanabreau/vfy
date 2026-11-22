use std::collections::BTreeMap;

use criterion::{BatchSize, Criterion, Throughput};
use rusqlite::Connection;
use vfy_core::data::CoverageData;
use vfy_core::encode_lines;
use vfy_core::semantic::SemanticData;

const FILES: usize = 128;
const CONTEXTS: usize = 64;

fn line_fixture(lines: &[i32]) -> (CoverageData, BTreeMap<(String, String), Vec<i32>>) {
    let mut expected = BTreeMap::new();
    let mut records = Vec::with_capacity(FILES * CONTEXTS);
    for file in 0..FILES {
        let path = format!("package/module_{file:05}.py");
        for context in 0..CONTEXTS {
            let context = format!("test_{context:03}|run");
            expected.insert((context.clone(), path.clone()), lines.to_vec());
            records.push((context, path.clone(), lines.to_vec()));
        }
    }
    (
        CoverageData::from_trace_records(
            false,
            records,
            Vec::new(),
            Vec::new(),
            SemanticData::default(),
        ),
        expected,
    )
}

pub(super) fn benchmark_line_persistence(criterion: &mut Criterion) {
    let dense: Vec<i32> = (1..=256).collect();
    let sparse_high = [1, 17, 65, 257, 1_025, 4_097, 16_385, 65_535];
    for (name, lines) in [
        ("dense_256", dense.as_slice()),
        ("sparse_high", &sparse_high),
    ] {
        let (data, expected) = line_fixture(lines);
        let mut prepared = Connection::open_in_memory().expect("benchmark database");
        data.write_connection(&mut prepared)
            .expect("benchmark line data writes");
        let expected_numbits = encode_lines(lines.iter().copied());
        let mut statement = prepared
            .prepare("SELECT numbits FROM line_bits")
            .expect("benchmark line bitmap query");
        let bitmaps: Vec<Vec<u8>> = statement
            .query_map([], |row| row.get(0))
            .expect("benchmark line bitmap rows")
            .collect::<Result<_, _>>()
            .expect("benchmark line bitmaps");
        assert_eq!(bitmaps.len(), FILES * CONTEXTS);
        assert!(bitmaps.iter().all(|bitmap| *bitmap == expected_numbits));
        drop(statement);
        let decoded = CoverageData::read_connection(&prepared).expect("benchmark line data reads");
        let actual: BTreeMap<_, _> = decoded
            .line_measurements()
            .map(|(context, path, lines)| {
                (
                    (context.to_owned(), path.to_owned()),
                    lines.into_iter().collect(),
                )
            })
            .collect();
        assert_eq!(actual, expected);

        let mut group =
            criterion.benchmark_group(format!("line_persistence/128_files_64_contexts/{name}"));
        group.sample_size(15);
        group.throughput(Throughput::Elements(
            u64::try_from(FILES * CONTEXTS * lines.len()).expect("benchmark size fits u64"),
        ));
        group.bench_function("write", |bencher| {
            bencher.iter_batched(
                || Connection::open_in_memory().expect("benchmark database"),
                |mut connection| {
                    data.write_connection(&mut connection)
                        .expect("benchmark line data writes");
                },
                BatchSize::SmallInput,
            );
        });
        group.bench_function("read", |bencher| {
            bencher.iter(|| {
                CoverageData::read_connection(&prepared).expect("benchmark line data reads")
            });
        });
        group.finish();
    }
}
