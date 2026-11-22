//! Worker-process count limits.

/// Return `min(pending, capacity)`, or one if either input is zero.
#[must_use]
pub const fn recommended_worker_count(pending: usize, capacity: usize) -> usize {
    if pending == 0 || capacity == 0 {
        1
    } else if pending < capacity {
        pending
    } else {
        capacity
    }
}

#[cfg(test)]
mod tests;
