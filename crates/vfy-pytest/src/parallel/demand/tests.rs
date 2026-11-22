use super::*;
use std::sync::{Arc, Barrier};

#[test]
fn independent_lease_tail_remains_available_to_waiting_workers() {
    for collected in [0, 1, 2, 7, 59, 64, 366, 981] {
        for workers in [1, 2, 4, 8] {
            let groups = DemandQueue::independent_groups(collected, workers);
            assert_eq!(
                groups
                    .iter()
                    .flat_map(|&(start, end)| start..end)
                    .collect::<Vec<_>>(),
                (0..collected).collect::<Vec<_>>()
            );
            let mut previous = 4;
            for (start, end) in groups {
                let size = end - start;
                assert!((1..=previous).contains(&size));
                if collected - start <= 2 * workers {
                    assert_eq!(size, 1, "the final cases must be individually claimable");
                }
                previous = size;
            }
        }
    }
}

#[test]
fn independent_workers_start_with_distinct_owned_work_then_claim_remaining_batches() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("independent queue directory");
    let controller = DemandQueue::initialize_independent(directory.path(), 59, 4)
        .expect("independent queue initializes");
    let queues = (0..4)
        .map(|worker| {
            DemandQueue::open_independent(directory.path(), 59, 4, worker)
                .expect("each independent worker opens its own seeded lease")
        })
        .collect::<Vec<_>>();
    let seeds = queues
        .iter()
        .map(|queue| queue.owned_indices().expect("owned seed"))
        .collect::<Vec<_>>();
    assert_eq!(
        seeds,
        vec![
            (0..4).collect::<Vec<_>>(),
            (4..8).collect(),
            (8..12).collect(),
            (12..16).collect()
        ]
    );
    assert!(!queues[0].owns(16).expect("membership before claim"));
    let DemandClaim::Lease(range) = queues[0].claim(directory.path(), 0).expect("next batch")
    else {
        panic!("unclaimed work must remain beyond the four seeds");
    };
    assert_eq!(range, 16..20);
    assert!(queues[0].owns(16).expect("membership after claim"));
    assert!(
        !queues[1]
            .owns(16)
            .expect("other worker cannot own the lease")
    );
    while let DemandClaim::Lease(_) = queues[0].claim(directory.path(), 0).expect("drain queue") {}
    let owners = controller
        .assignments(directory.path())
        .expect("authoritative ledger");
    let mut all = owners
        .iter()
        .flatten()
        .cloned()
        .flatten()
        .collect::<Vec<_>>();
    all.sort_unstable();
    assert_eq!(all, (0..59).collect::<Vec<_>>());
    assert_eq!(queues[0].claimed_count(), 47);
}

#[test]
fn fewer_cases_than_workers_still_have_one_seed_per_admitted_worker() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("small independent queue directory");
    DemandQueue::initialize_independent(directory.path(), 2, 2).expect("small queue");
    for worker in 0..2 {
        let queue =
            DemandQueue::open_independent(directory.path(), 2, 2, worker).expect("seed owner");
        assert_eq!(queue.owned_indices().expect("seeded case"), vec![worker]);
        assert!(matches!(
            queue
                .claim(directory.path(), worker)
                .expect("queue exhausted"),
            DemandClaim::Finished
        ));
    }
}

#[test]
fn concurrent_fresh_descriptor_claims_preserve_variable_scope_epochs() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("claim directory");
    let groups = vec![(0, 1), (1, 33), (33, 34), (34, 64), (64, 65), (65, 97)];
    let queue = DemandQueue::initialize(directory.path(), groups.clone(), 97, 4)
        .expect("variable epochs initialize");
    let barrier = Arc::new(Barrier::new(4));
    let threads = (0..4)
        .map(|worker| {
            let directory = directory.path().to_path_buf();
            let barrier = barrier.clone();
            let groups = groups.clone();
            std::thread::spawn(move || {
                // Separate process-local counters, and fresh file descriptions for each claim.
                let queue = DemandQueue {
                    groups: groups.into_iter().map(|(start, end)| start..end).collect(),
                    workers: 4,
                    claimed: AtomicUsize::new(0),
                    startup: None,
                    initial: Mutex::new(None),
                    owned: Mutex::new(Vec::new()),
                };
                let DemandClaim::Lease(first) =
                    queue.claim(&directory, worker).expect("first claim")
                else {
                    panic!("first scope epoch should be leased");
                };
                barrier.wait();
                let mut leases = vec![first];
                while let DemandClaim::Lease(range) =
                    queue.claim(&directory, worker).expect("next claim")
                {
                    leases.push(range);
                }
                assert_eq!(
                    queue.claimed_count(),
                    leases.iter().map(ExactSizeIterator::len).sum::<usize>()
                );
                leases
            })
        })
        .collect::<Vec<_>>();
    let mut leases = threads
        .into_iter()
        .flat_map(|thread| thread.join().expect("claimant exits"))
        .collect::<Vec<_>>();
    leases.sort_by_key(|range| range.start);
    let expected = groups
        .into_iter()
        .map(|(start, end)| start..end)
        .collect::<Vec<_>>();
    assert_eq!(
        leases, expected,
        "epochs must neither split nor repeat under contention"
    );
    let assignments = queue
        .assignments(directory.path())
        .expect("joined ledger validates");
    let mut committed = assignments.into_iter().flatten().collect::<Vec<_>>();
    committed.sort_by_key(|range| range.start);
    assert_eq!(
        committed, expected,
        "merge ownership must agree with actual leases"
    );
}

#[test]
fn incomplete_lease_commit_and_invalid_owner_cannot_publish_results() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("claim directory");
    for (cursor, owner) in [(0, 0), (1, 2), (3, UNCLAIMED)] {
        let queue = DemandQueue::initialize(directory.path(), vec![(0, 1), (1, 33)], 33, 2)
            .expect("queue initializes");
        let mut file = OpenOptions::new()
            .write(true)
            .open(directory.path().join(QUEUE_FILE))
            .expect("ledger opens");
        file.write_all(&u64::to_le_bytes(cursor))
            .expect("cursor written");
        file.write_all(&owner.to_le_bytes()).expect("owner written");
        assert!(queue.assignments(directory.path()).is_err());
    }
    let queue = DemandQueue::initialize(directory.path(), vec![(0, 1), (1, 33)], 33, 2)
        .expect("queue initializes");
    OpenOptions::new()
        .write(true)
        .open(directory.path().join(QUEUE_FILE))
        .expect("ledger opens")
        .set_len(WORD_BYTES * 2)
        .expect("ledger truncated");
    assert!(queue.assignments(directory.path()).is_err());
}

#[test]
fn first_completion_rejects_out_of_range_cursor_before_mutation() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("claim directory");
    let queue = DemandQueue {
        groups: vec![0..16, 16..32],
        workers: 2,
        claimed: AtomicUsize::new(0),
        initial: Mutex::new(None),
        owned: Mutex::new(Vec::new()),
        startup: Some(StartupGrace {
            limit_ms: 5000,
            origin: Instant::now(),
        }),
    };
    // Valid first-owner and startup fields isolate the corrupt three-of-two cursor.
    let bytes = [3, 0, 1, 0, UNCLAIMED]
        .into_iter()
        .flat_map(u64::to_le_bytes)
        .collect::<Vec<_>>();
    let ledger = directory.path().join(QUEUE_FILE);
    std::fs::write(&ledger, &bytes).expect("corrupt ledger written");
    assert!(queue.complete_first(directory.path(), 0).is_err());
    assert_eq!(
        std::fs::read(&ledger).expect("ledger remains readable"),
        bytes,
        "invalid cursor must not publish the completion header"
    );
}
