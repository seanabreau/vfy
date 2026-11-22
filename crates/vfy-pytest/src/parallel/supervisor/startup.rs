//! Worker spawning, prespawn adoption, and supervisor startup sequencing.

use super::{ParallelSupervisor, StreamBoundaries};
use crate::parallel::{
    CollectedBootstrap, CollectionRoute, NativeParallelSession, NativeParallelWorker,
    POLL_INTERVAL, ParallelChild, ParallelSignalGuard, WorkerCompletionRoute, cancellation,
    coverage_base, independent, io_error, mutation, stop_prespawned, surplus_path,
    validate_fork_resources, worker_exit_code, write_json_atomic,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes};
use rustix::process::Pid;
use std::borrow::Cow;
use std::collections::HashMap;
use std::fs::{self, File};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Instant;
use tempfile::Builder;

impl<'a> ParallelSupervisor<'a> {
    pub(super) fn retain_child(&mut self, index: usize, pid: Pid, child: ParallelChild) {
        self.process_groups[index] = Some(pid);
        self.worker_pids[index] = Some(pid);
        self.children[index] = Some(child);
    }

    pub(in crate::parallel) fn start(
        session: &'a NativeParallelSession,
        executable: &str,
        args: &[String],
        environment: &HashMap<String, String>,
        invocation_dir: &Path,
    ) -> PyResult<Self> {
        let mut supervisor = Self::prepare(session)?;
        supervisor.spawn_workers(0, executable, args, environment, invocation_dir)?;
        Ok(supervisor)
    }

    pub(in crate::parallel) fn spawn_workers(
        &mut self,
        start: usize,
        executable: &str,
        args: &[String],
        environment: &HashMap<String, String>,
        invocation_dir: &Path,
    ) -> PyResult<()> {
        for index in start..self.children.len() {
            let (pid, child) =
                self.spawn_worker(index, executable, args, environment, invocation_dir)?;
            self.adopt_spawned(index, pid, child, environment);
        }
        Ok(())
    }

    /// Start workers `1..workers` before worker zero publishes its collection.
    pub(in crate::parallel) fn prespawn_workers(
        &mut self,
        workers: usize,
        executable: &str,
        args: &[String],
        environment: &HashMap<String, String>,
        invocation_dir: &Path,
    ) -> PyResult<()> {
        self.speculative_coverage_base = coverage_base(environment).map(str::to_owned);
        for index in self.children.len()..workers {
            let (pid, child) =
                self.spawn_worker(index, executable, args, environment, invocation_dir)?;
            self.prespawned.push((index, pid, child));
        }
        Ok(())
    }

    /// Adopt prespawned workers below the collected width. Each worker beyond it
    /// is told it has no assignment, so it finishes its session without tests.
    pub(in crate::parallel) fn adopt_prespawned(
        &mut self,
        environment: &HashMap<String, String>,
    ) -> PyResult<()> {
        let width = self.children.len();
        self.prespawned.reverse();
        while let Some((index, pid, child)) = self.prespawned.pop() {
            if index < width {
                self.adopt_spawned(index, pid, child, environment);
            } else {
                self.surplus.push((index, pid, child));
                vfy_core::atomic_write(&surplus_path(self.directory.path(), index), b"")
                    .map_err(io_error)?;
            }
        }
        Ok(())
    }

    pub(in crate::parallel) fn adopt_all_prespawned(
        &mut self,
        workers: usize,
        environment: &HashMap<String, String>,
    ) {
        if self.prespawned.is_empty() {
            return;
        }
        self.children.resize_with(workers, || None);
        self.process_groups.resize(workers, None);
        self.worker_pids.resize(workers, None);
        self.statuses.resize(workers, None);
        self.completed_workers.resize(workers, None);
        self.authenticated_completions.resize_with(workers, || None);
        for (index, pid, child) in std::mem::take(&mut self.prespawned) {
            self.adopt_spawned(index, pid, child, environment);
        }
    }

    pub(in crate::parallel) fn discard_prespawned(&mut self) {
        for (index, pid, child) in std::mem::take(&mut self.prespawned)
            .into_iter()
            .chain(std::mem::take(&mut self.surplus))
        {
            stop_prespawned(pid, child);
            if let Some(base) = &self.speculative_coverage_base {
                let _ = std::fs::remove_file(format!("{base}.worker{index}"));
            }
        }
    }

    fn adopt_spawned(
        &mut self,
        index: usize,
        pid: Pid,
        child: Child,
        environment: &HashMap<String, String>,
    ) {
        if let Some(base) = coverage_base(environment) {
            self.coverage_shards
                .push(PathBuf::from(format!("{base}.worker{index}")));
        }
        self.retain_child(index, pid, ParallelChild::Spawned(child));
    }

    fn spawn_worker(
        &self,
        index: usize,
        executable: &str,
        args: &[String],
        environment: &HashMap<String, String>,
        invocation_dir: &Path,
    ) -> PyResult<(Pid, Child)> {
        #[expect(
            clippy::disallowed_methods,
            reason = "worker output streams into private log files while the child runs"
        )]
        let (stdout, stderr) = (
            File::create(self.directory.path().join(format!("stdout.{index}")))
                .map_err(io_error)?,
            File::create(self.directory.path().join(format!("stderr.{index}")))
                .map_err(io_error)?,
        );
        let mut command = Command::new(executable);
        command
            .current_dir(invocation_dir)
            .env_clear()
            .args(args)
            .envs(environment)
            .env("VFY_NATIVE_PARALLEL_DIR", self.directory.path())
            .env("VFY_NATIVE_PARALLEL_INDEX", index.to_string())
            .env("VFY_NATIVE_WORKERS", "1")
            .env(
                "VFY_TEST_RESULTS",
                self.directory
                    .path()
                    .join(format!("worker{index}.observer.json")),
            )
            .env_remove("PYTEST_NATIVE_RESULT")
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        if let Some(base) = coverage_base(environment) {
            command.env("COVERAGE_FILE", format!("{base}.worker{index}"));
        } else {
            command.env(
                "COVERAGE_FILE",
                self.directory
                    .path()
                    .join(format!(".coverage.worker{index}")),
            );
        }
        if environment.contains_key("PYTEST_NATIVE_STAGE_OUTPUT") {
            command.env(
                "PYTEST_NATIVE_STAGE_OUTPUT",
                self.directory.path().join(format!("stage.{index}.json")),
            );
        }
        if let Some(identity) = &self.invocation_identity {
            command.env(independent::NONCE_ENV, identity.nonce());
            #[cfg(target_os = "linux")]
            command.env(independent::PARENT_PID_ENV, std::process::id().to_string());
        }
        command.process_group(0);
        let child = command.spawn().map_err(io_error)?;
        Ok((Pid::from_child(&child), child))
    }

    pub(in crate::parallel) fn prepare(session: &'a NativeParallelSession) -> PyResult<Self> {
        fs::create_dir_all(&session.directory).map_err(io_error)?;
        let directory = Builder::new()
            .prefix("native-pytest-parallel-")
            .tempdir_in(&session.directory)
            .map_err(io_error)?;
        let plan = &session.plan;
        let invocation_identity = session
            .independent_protocol
            .then(|| {
                if let Some(execution) = session.fresh_execution.as_ref() {
                    match execution.purpose() {
                        independent::InvocationPurpose::FreshCollection => {
                            independent::BootstrapIdentity::create_fresh_collection(
                                directory.path(),
                                plan.assignments.len(),
                            )
                        }
                        _ => independent::BootstrapIdentity::create_fresh(
                            directory.path(),
                            plan.assignments.len(),
                            execution.mutation.as_ref(),
                        ),
                    }
                } else {
                    independent::BootstrapIdentity::create(directory.path(), plan.assignments.len())
                }
            })
            .transpose()?;
        match session.collection_route {
            CollectionRoute::Controller => {
                write_json_atomic(&directory.path().join("plan.json"), plan).map_err(io_error)?;
            }
            CollectionRoute::Workers => write_json_atomic(
                &directory.path().join("bootstrap.json"),
                &plan.assignments.len(),
            )
            .map_err(io_error)?,
        }
        vfy_core::atomic_write(
            &directory.path().join("failure-budget"),
            format!("{}\n", plan.initial_failures).as_bytes(),
        )
        .map_err(io_error)?;

        // The first fresh collector determines capacity without a controller
        // collection replica; it remains blocked until all admitted workers agree.
        let worker_count = match session.collection_route {
            CollectionRoute::Controller => plan.assignments.len(),
            CollectionRoute::Workers => 1,
        };
        Ok(Self {
            directory,
            plan: Cow::Borrowed(plan),
            collection_route: session.collection_route,
            result_mode: session.result_mode,
            collection_error_policy: session.collection_error_policy,
            completion_route: match session.collection_route {
                CollectionRoute::Controller => WorkerCompletionRoute::Unrecorded,
                CollectionRoute::Workers => WorkerCompletionRoute::PostUnconfigure,
            },
            invocation_identity,
            fresh_execution: session.fresh_execution.as_ref(),
            fresh_inputs: None,
            fresh_collection_nodeids: None,
            fresh_tests: session
                .fresh_execution
                .as_ref()
                .map_or_else(Vec::new, |execution| execution.tests.clone()),
            fresh_deadline: session
                .fresh_execution
                .as_ref()
                .map(mutation::FreshExecution::deadline),
            fresh_timed_out: false,
            fresh_abrupt_exit: None,
            authenticated_completions: vec![None; worker_count],
            completed_workers: vec![None; worker_count],
            control_drain: None,
            cancellation: cancellation::Cancellation::default(),
            parent_interruption: None,
            checked_cleanup: None,
            demand: None,
            children: (0..worker_count).map(|_| None).collect(),
            process_groups: vec![None; worker_count],
            worker_pids: vec![None; worker_count],
            statuses: (0..worker_count).map(|_| None).collect(),
            coverage_shards: Vec::new(),
            prespawned: Vec::new(),
            surplus: Vec::new(),
            speculative_coverage_base: None,
            stream_boundaries: StreamBoundaries {
                stdout_ends_with_newline: true,
                stderr_ends_with_newline: true,
            },
            released: false,
            complete: false,
            progress_config: session.progress_config.as_ref(),
            progress: None,
        })
    }

    fn bootstrap_collected_child(
        &self,
        py: Python<'_>,
        index: usize,
        launch: &CollectedBootstrap<'_, '_>,
    ) -> PyResult<i32> {
        launch.signals.signal.bind(py).call_method1(
            "signal",
            (
                launch.signals.signum.bind(py),
                launch.signals.previous.bind(py),
            ),
        )?;
        launch
            .output
            .call1((self.directory.path().to_string_lossy().as_ref(), index))?;
        let worker = NativeParallelWorker {
            directory: self.directory.path().to_path_buf(),
            index,
            indices: self.plan.assignments[index].clone(),
            maxfail: self.plan.maxfail,
            demand: self.demand.clone(),
            coverage_shard: self.coverage_shards.get(index).cloned(),
            progress: Mutex::new(None),
        };
        if let Some(identity) = &self.invocation_identity {
            let environment = py.import("os")?.getattr("environ")?;
            environment.set_item(independent::NONCE_ENV, identity.nonce())?;
            #[cfg(target_os = "linux")]
            environment.set_item(independent::PARENT_PID_ENV, &launch.controller_pid)?;
        }
        launch
            .callback
            .call1((
                self.directory.path().to_string_lossy().as_ref(),
                index,
                Py::new(py, worker)?,
            ))?
            .extract()
    }

    pub(in crate::parallel) fn start_collected(
        &mut self,
        py: Python<'_>,
        bootstrap: &Bound<'_, PyAny>,
        signals: &ParallelSignalGuard,
        allowed_fds: &[i32],
        deadline: Instant,
    ) -> PyResult<()> {
        self.completion_route = WorkerCompletionRoute::PostUnconfigure;
        let os = py.import("os")?;
        let posix = py.import("posix")?;
        let pipe = posix.getattr("pipe")?;
        let fork = posix.getattr("fork")?;
        let close = posix.getattr("close")?;
        let read = posix.getattr("read")?;
        let write = posix.getattr("write")?;
        let setpgid = posix.getattr("setpgid")?;
        let child_exit = posix.getattr("_exit")?;
        let bind_output = py
            .import("vfy.independent.stdio")?
            .getattr("bind_worker_output")?;
        let launch = CollectedBootstrap {
            callback: bootstrap,
            output: &bind_output,
            signals,
            #[cfg(target_os = "linux")]
            controller_pid: std::process::id().to_string(),
        };
        for index in 0..self.plan.assignments.len() {
            self.check_startup_deadline(py, Some(deadline))?;
            py.check_signals()?;
            let owned_children = self
                .process_groups
                .iter()
                .flatten()
                .copied()
                .collect::<Vec<_>>();
            validate_fork_resources(allowed_fds, &owned_children)?;
            for (name, function) in [
                ("fork", &fork),
                ("pipe", &pipe),
                ("close", &close),
                ("read", &read),
                ("write", &write),
                ("setpgid", &setpgid),
                ("_exit", &child_exit),
            ] {
                if !os.getattr(name)?.is(function)
                    || function.getattr("__name__")?.extract::<String>()? != name
                {
                    return Err(PyValueError::new_err(format!(
                        "collected fork workers require the original os.{name}"
                    )));
                }
            }
            let (read_fd, write_fd): (i32, i32) = pipe.call0()?.extract()?;
            let pid = match fork.call0().and_then(|value| value.extract::<i32>()) {
                Ok(pid) => pid,
                Err(error) => {
                    let _ = close.call1((read_fd,));
                    let _ = close.call1((write_fd,));
                    return Err(error);
                }
            };
            if pid == 0 {
                let code = (|| -> PyResult<i32> {
                    close.call1((write_fd,))?;
                    let acknowledgment = read.call1((read_fd, 1))?;
                    close.call1((read_fd,))?;
                    if acknowledgment.cast::<PyBytes>()?.as_bytes() != b"1" {
                        return Ok(3);
                    }
                    self.bootstrap_collected_child(py, index, &launch)
                })()
                .unwrap_or_else(|error| {
                    error.print(py);
                    3
                });
                // os._exit prevents child failures from dropping the inherited supervisor
                // or removing its TempDir.
                let _ = child_exit.call1((code,));
                std::process::exit(3);
            }
            if u32::try_from(pid).ok() == Some(std::process::id()) {
                return Err(PyRuntimeError::new_err(
                    "os.fork returned the controller process ID",
                ));
            }
            let process_id = Pid::from_raw(pid)
                .ok_or_else(|| PyRuntimeError::new_err("os.fork returned an invalid process ID"))?;
            self.retain_child(index, process_id, ParallelChild::Forked(process_id));
            let registration = setpgid.call1((pid, pid));
            let acknowledgment =
                registration.and_then(|_| write.call1((write_fd, PyBytes::new(py, b"1"))));
            let _ = close.call1((read_fd,));
            let _ = close.call1((write_fd,));
            acknowledgment?;
            py.check_signals()?;
        }
        Ok(())
    }

    pub(in crate::parallel) fn await_compilation(&mut self, py: Python<'_>) -> PyResult<()> {
        self.await_compilation_until(py, None)
    }

    fn check_startup_deadline(
        &mut self,
        py: Python<'_>,
        deadline: Option<Instant>,
    ) -> PyResult<()> {
        if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            return Err(self.abort_error(
                py,
                "collected fork workers exceeded their startup deadline".to_owned(),
                Some(3),
            ));
        }
        Ok(())
    }

    pub(in crate::parallel) fn await_compilation_until(
        &mut self,
        py: Python<'_>,
        deadline: Option<Instant>,
    ) -> PyResult<()> {
        loop {
            self.check_fresh_deadline(py)?;
            self.check_startup_deadline(py, deadline)?;
            if let Err(error) = py.check_signals() {
                self.abort("parent interrupted");
                return Err(error);
            }
            if (0..self.children.len()).all(|index| {
                self.directory
                    .path()
                    .join(format!("ready.{index}"))
                    .is_file()
            }) {
                return Ok(());
            }
            for index in 0..self.children.len() {
                if let Some(child) = self.children[index].as_mut()
                    && let Some(status) = child.try_wait().map_err(io_error)?
                {
                    self.statuses[index] = Some(status);
                    self.children[index] = None;
                    self.check_finished_worker(py, index, status)?;
                    return Err(self.abort_error(
                        py,
                        format!("native parallel worker {index} exited before all workers compiled (status {status})"),
                        Some(worker_exit_code(status)),
                    ));
                }
            }
            py.detach(|| thread::sleep(POLL_INTERVAL));
        }
    }
}
