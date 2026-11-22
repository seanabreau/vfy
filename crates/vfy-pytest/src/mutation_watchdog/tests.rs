use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use super::*;

fn sleeper() -> Child {
    Command::new("sleep")
        .arg("30")
        .process_group(0)
        .spawn()
        .expect("sleep starts")
}

fn watched(child: &Child, seconds: u64) -> Vec<WatchedProcess> {
    vec![WatchedProcess {
        pid: Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap(),
        deadline: Instant::now() + Duration::from_secs(seconds),
    }]
}

fn running() -> StopSignal {
    Arc::new((Mutex::new(false), Condvar::new()))
}

#[test]
fn expired_deadline_kills_the_child_group_without_reaping_it_first() {
    let mut child = sleeper();
    let expired = watch(watched(&child, 0), None, &running()).unwrap();
    assert_eq!(expired, vec![i32::try_from(child.id()).unwrap()]);
    assert_eq!(child.wait().unwrap().signal(), Some(9));
}

#[test]
fn child_that_exited_before_its_deadline_is_cleaned_up_but_not_reported() {
    let mut child = Command::new("true").process_group(0).spawn().unwrap();
    let jobs = watched(&child, 30);
    let expired = watch(jobs, None, &running()).unwrap();
    assert!(expired.is_empty());
    assert!(child.wait().unwrap().success());
}

#[test]
fn child_reaped_by_collection_code_is_released() {
    let mut child = Command::new("true").process_group(0).spawn().unwrap();
    let jobs = watched(&child, 30);
    child.wait().unwrap();
    assert!(watch(jobs, None, &running()).unwrap().is_empty());
}

#[test]
fn stop_request_ends_supervision_of_a_live_child() {
    let mut child = sleeper();
    let stop = running();
    *stop.0.lock().unwrap() = true;
    assert!(watch(watched(&child, 30), None, &stop).unwrap().is_empty());
    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn forked_copy_finishes_without_touching_the_vanished_watcher_thread() {
    let mut inherited = MutationProcessWatchdog {
        owner: std::process::id() + 1,
        stop: running(),
        thread: None,
        expired: Vec::new(),
    };
    assert!(inherited.finish_inner().unwrap().is_empty());
}
