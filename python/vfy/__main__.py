import os

_profiler = None
if os.getenv("VFY_PROFILE_DIR"):
    import atexit
    import cProfile

    _profiler = cProfile.Profile()
    _profiler.enable()

    def _write_profile() -> None:
        assert _profiler is not None
        _profiler.disable()
        _profiler.dump_stats(os.path.join(os.environ["VFY_PROFILE_DIR"], "controller.pstats"))

    atexit.register(_write_profile)

os.environ["VFY_IN_PROCESS_PYTEST"] = "1"


def _run() -> int:
    from .entry import main  # noqa: PLC0415 - profiling starts before the CLI import

    return main()


raise SystemExit(_run())
