use super::recommended_worker_count;

#[test]
fn bounds_workers_by_work_and_capacity() {
    assert_eq!(recommended_worker_count(1, 8), 1);
    assert_eq!(recommended_worker_count(3, 8), 3);
    assert_eq!(recommended_worker_count(20, 8), 8);
    assert_eq!(recommended_worker_count(0, 8), 1);
    assert_eq!(recommended_worker_count(3, 0), 1);
}
