use super::classify;
use pyo3::prelude::*;
use pyo3::types::{PyModule, PyTuple};
use vfy_core::test_plan::FixtureExecution;

fn inspect_kind(callable: &Bound<'_, PyAny>) -> PyResult<FixtureExecution> {
    let inspect = callable.py().import("inspect")?;
    for (predicate, kind) in [
        ("iscoroutinefunction", FixtureExecution::AsyncCoroutine),
        ("isasyncgenfunction", FixtureExecution::AsyncGenerator),
        ("isgeneratorfunction", FixtureExecution::Generator),
    ] {
        if inspect.call_method1(predicate, (callable,))?.is_truthy()? {
            return Ok(kind);
        }
    }
    Ok(FixtureExecution::Callable)
}

#[test]
fn callable_kinds_match_inspect_for_functions_and_wrapped_protocols() -> PyResult<()> {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy")?;
        crate::hooks::nativeop::initialize(py)?;
        let module = PyModule::from_code(
            py,
            c"
import functools
import inspect
import types
from vfy._native._pytest import direct_parameter

def plain(): pass
def generator(): yield
async def coroutine(): pass
async def async_generator(): yield

@functools.wraps(coroutine)
def wrapped(): return coroutine()

@inspect.markcoroutinefunction
def marked(): return coroutine()

@functools.wraps(marked)
def wrapped_marked(): return coroutine()

def false_marker(): pass
false_marker._is_coroutine_marker = object()

class Methods:
    plain = staticmethod(plain)
    generator = generator
    coroutine = coroutine
    async_generator = async_generator
    marked = marked
    partial_coroutine = functools.partialmethod(coroutine)

class Callable:
    async def __call__(self): pass

class Functionlike:
    __name__ = 'functionlike'
    __code__ = coroutine.__code__
    __defaults__ = None
    __kwdefaults__ = None
    __annotations__ = {}
    def __call__(self): return coroutine()

cases = [(f.__name__, f) for f in (
    plain, generator, coroutine, async_generator, wrapped, marked,
    wrapped_marked, false_marker, Callable, len, iter, [].append, direct_parameter,
)]
cases.extend((('callable instance', Callable()), ('functionlike', Functionlike())))
owner = Methods()
cases.append(('method wrapping partial', types.MethodType(functools.partial(coroutine), owner)))
for name in ('plain', 'generator', 'coroutine', 'async_generator', 'marked', 'partial_coroutine'):
    value = getattr(owner, name)
    cases.extend(((name, value), ('partial ' + name, functools.partial(value))))
",
            c"callable_kinds.py",
            c"callable_kinds",
        )?;
        for case in module.getattr("cases")?.try_iter()? {
            let case = case?.cast_into::<PyTuple>()?;
            let name = case.get_item(0)?.extract::<String>()?;
            let callable = case.get_item(1)?;
            assert_eq!(classify(&callable)?, inspect_kind(&callable)?, "{name}");
        }
        Ok(())
    })
}

#[test]
fn classification_rechecks_code_and_coroutine_marker_identity() -> PyResult<()> {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy")?;
        crate::hooks::nativeop::initialize(py)?;
        let module = PyModule::from_code(
            py,
            c"
def live(): pass
def plain(): pass
def generator(): yield
async def coroutine(): pass
async def async_generator(): yield
class Owner:
    method = live
bound = Owner().method
replacements = (plain, coroutine, generator, async_generator, plain)
",
            c"live_callable_kind.py",
            c"live_callable_kind",
        )?;
        let callable = module.getattr("live")?;
        let bound = module.getattr("bound")?;
        let inspect = py.import("inspect")?;
        for replacement in module.getattr("replacements")?.try_iter()? {
            callable.setattr("__code__", replacement?.getattr("__code__")?)?;
            assert_eq!(classify(&callable)?, inspect_kind(&callable)?);
            assert_eq!(classify(&bound)?, inspect_kind(&bound)?);
            inspect.call_method1("markcoroutinefunction", (&callable,))?;
            assert_eq!(classify(&callable)?, FixtureExecution::AsyncCoroutine);
            assert_eq!(classify(&bound)?, FixtureExecution::AsyncCoroutine);
            callable.setattr("_is_coroutine_marker", py.None())?;
            assert_eq!(classify(&callable)?, inspect_kind(&callable)?);
            assert_eq!(classify(&bound)?, inspect_kind(&bound)?);
            callable.delattr("_is_coroutine_marker")?;
            assert_eq!(classify(&callable)?, inspect_kind(&callable)?);
            assert_eq!(classify(&bound)?, inspect_kind(&bound)?);
        }
        Ok(())
    })
}
