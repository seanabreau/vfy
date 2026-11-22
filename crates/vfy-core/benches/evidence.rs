use std::hint::black_box;

use criterion::{BatchSize, BenchmarkId, Criterion, criterion_group, criterion_main};
use vfy_core::TraceStore;

fn benchmark_mixed_paths_and_contexts(criterion: &mut Criterion) {
    let events: Vec<_> = (0..64)
        .flat_map(|context| {
            (0..64).flat_map(move |path| {
                [1, 64, 65, 4_096].map(move |line| {
                    (
                        format!("tests/test_{context:03}.py::test_value|run"),
                        format!("package/module_{path:03}.py"),
                        line,
                    )
                })
            })
        })
        .collect();
    let mut compact = TraceStore::new(false);
    for (context, path, line) in &events {
        compact.add_line(context, path, *line);
    }
    let records = compact.line_records();
    let expected_records: Vec<_> = (0..64)
        .flat_map(|context| {
            (0..64).map(move |path| {
                (
                    format!("tests/test_{context:03}.py::test_value|run"),
                    format!("package/module_{path:03}.py"),
                    vec![1, 64, 65, 4_096],
                )
            })
        })
        .collect();
    let mut expected_records = expected_records;
    expected_records.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    assert_eq!(records, expected_records);

    let mut group = criterion.benchmark_group("trace/line/many-paths-contexts");
    group.bench_function(BenchmarkId::new("compact", events.len()), |bencher| {
        bencher.iter_batched(
            || TraceStore::new(false),
            |mut store| {
                for (context, path, line) in &events {
                    store.add_line(context, path, *line);
                }
                black_box(store.line_records());
            },
            BatchSize::SmallInput,
        );
    });
    group.finish();
}

fn benchmark_evidence(criterion: &mut Criterion) {
    let mut compact_repeated = TraceStore::new(false);
    for _ in 0..10_000 {
        compact_repeated.add_line("test_module.py::test_value|run", "package/module.py", 41);
    }
    assert_eq!(
        compact_repeated.line_records(),
        vec![(
            "test_module.py::test_value|run".to_owned(),
            "package/module.py".to_owned(),
            vec![41]
        )]
    );

    let mut compact_sparse = TraceStore::new(false);
    for context in 0..1_000 {
        let context = format!("tests/test_{context}.py::test_value|run");
        for line in [1, 64, 65, 4_096] {
            compact_sparse.add_line(&context, "package/module.py", line);
        }
    }
    let sparse_records = compact_sparse.line_records();
    let expected_sparse_records: Vec<_> = (0..1_000)
        .map(|context| {
            (
                format!("tests/test_{context}.py::test_value|run"),
                "package/module.py".to_owned(),
                vec![1, 64, 65, 4_096],
            )
        })
        .collect();
    let mut expected_sparse_records = expected_sparse_records;
    expected_sparse_records
        .sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    assert_eq!(sparse_records, expected_sparse_records);

    let mut repeated = criterion.benchmark_group("trace/line/repeated");
    repeated.bench_function(BenchmarkId::new("compact", 10_000), |bencher| {
        bencher.iter_batched(
            || TraceStore::new(false),
            |mut store| {
                for _ in 0..10_000 {
                    store.add_line("test_module.py::test_value|run", "package/module.py", 41);
                }
                black_box(store.line_records());
            },
            BatchSize::SmallInput,
        );
    });
    repeated.finish();

    let mut sparse = criterion.benchmark_group("trace/line/sparse-contexts");
    sparse.bench_function(BenchmarkId::new("compact", 1_000), |bencher| {
        bencher.iter_batched(
            || TraceStore::new(false),
            |mut store| {
                for context in 0..1_000 {
                    let context = format!("tests/test_{context}.py::test_value|run");
                    for line in [1, 64, 65, 4_096] {
                        store.add_line(&context, "package/module.py", line);
                    }
                }
                black_box(store.line_records());
            },
            BatchSize::SmallInput,
        );
    });
    sparse.finish();

    benchmark_mixed_paths_and_contexts(criterion);

    criterion.bench_function("trace/branch/repeated", |bencher| {
        bencher.iter_batched(
            || TraceStore::new(true),
            |mut store| {
                for _ in 0..10_000 {
                    store.add_arc(
                        "test_module.py::test_value|run",
                        "package/module.py",
                        41,
                        42,
                    );
                }
                black_box(store.arc_records());
            },
            BatchSize::SmallInput,
        );
    });
}

criterion_group!(benches, benchmark_evidence);
criterion_main!(benches);
