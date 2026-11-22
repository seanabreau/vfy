//! Execnet serialization for values whose encoding matches the upstream serializer.

use std::collections::HashSet;

use pyo3::exceptions::PyUnicodeEncodeError;
use pyo3::prelude::*;
use pyo3::pyclass::{PyTraverseError, PyVisit};
use pyo3::types::{
    PyBool, PyBytes, PyCFunction, PyComplex, PyDict, PyFloat, PyFrozenSet, PyFunction, PyInt,
    PyList, PySet, PyString, PyTuple, PyType,
};

const MAX_DEPTH: usize = 128;
const MAX_VISITS: usize = 1_000_000;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Admission {
    Native,
    Upstream,
}

struct WatchedAttribute {
    owner: Py<PyAny>,
    name: &'static str,
    value: Py<PyAny>,
    code: Option<Py<PyAny>>,
}

struct GraphAdmission {
    ancestors: HashSet<usize>,
    visits: usize,
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeExecnetEncoder {
    upstream: Py<PyAny>,
    dispatch: Py<PyDict>,
    methods: Vec<(Py<PyType>, Py<PyAny>)>,
    watched: Vec<WatchedAttribute>,
    namespace: Py<PyDict>,
    globals: Vec<(&'static str, Py<PyAny>)>,
    initial_admission: Admission,
}

fn kind(value: &Bound<'_, PyAny>) -> Option<&'static str> {
    if value.is_none() {
        Some("NoneType")
    } else if value.is_exact_instance_of::<PyBool>() {
        Some("bool")
    } else if value.is_exact_instance_of::<PyInt>() {
        Some("int")
    } else if value.is_exact_instance_of::<PyString>() {
        Some("str")
    } else if value.is_exact_instance_of::<PyBytes>() {
        Some("bytes")
    } else if value.is_exact_instance_of::<PyFloat>() {
        Some("float")
    } else if value.is_exact_instance_of::<PyComplex>() {
        Some("complex")
    } else if value.is_exact_instance_of::<PyList>() {
        Some("list")
    } else if value.is_exact_instance_of::<PyDict>() {
        Some("dict")
    } else if value.is_exact_instance_of::<PyTuple>() {
        Some("tuple")
    } else if value.is_exact_instance_of::<PySet>() {
        Some("set")
    } else if value.is_exact_instance_of::<PyFrozenSet>() {
        Some("frozenset")
    } else {
        None
    }
}

impl NativeExecnetEncoder {
    fn transport_admission(&self, py: Python<'_>) -> PyResult<Admission> {
        if self.initial_admission == Admission::Upstream {
            return Ok(Admission::Upstream);
        }
        for (name, expected) in &self.globals {
            if self
                .namespace
                .bind(py)
                .get_item(name)?
                .is_none_or(|value| !value.is(expected.bind(py)))
            {
                return Ok(Admission::Upstream);
            }
        }
        for (kind, expected) in &self.methods {
            if self
                .dispatch
                .bind(py)
                .get_item(kind.bind(py))?
                .is_some_and(|value| !value.is(expected.bind(py)))
            {
                return Ok(Admission::Upstream);
            }
        }
        for watched in &self.watched {
            let current = watched.owner.bind(py).getattr(watched.name)?;
            if !current.is(watched.value.bind(py)) {
                return Ok(Admission::Upstream);
            }
            if let Some(code) = &watched.code
                && !current.getattr("__code__")?.is(code.bind(py))
            {
                return Ok(Admission::Upstream);
            }
        }
        Ok(Admission::Native)
    }

    fn snapshot_serializer_methods(
        &mut self,
        py: Python<'_>,
        serializer: &Bound<'_, PyAny>,
        filename: &Bound<'_, PyAny>,
    ) -> PyResult<Admission> {
        let names = [
            ("NoneType", "save_NoneType"),
            ("bool", "save_bool"),
            ("int", "save_int"),
            ("str", "save_str"),
            ("bytes", "save_bytes"),
            ("float", "save_float"),
            ("complex", "save_complex"),
            ("list", "save_list"),
            ("dict", "save_dict"),
            ("tuple", "save_tuple"),
            ("set", "save_set"),
            ("frozenset", "save_frozenset"),
        ];
        let types = [
            py.None().bind(py).get_type(),
            py.get_type::<PyBool>(),
            py.get_type::<PyInt>(),
            py.get_type::<PyString>(),
            py.get_type::<PyBytes>(),
            py.get_type::<PyFloat>(),
            py.get_type::<PyComplex>(),
            py.get_type::<PyList>(),
            py.get_type::<PyDict>(),
            py.get_type::<PyTuple>(),
            py.get_type::<PySet>(),
            py.get_type::<PyFrozenSet>(),
        ];
        for (kind, (_, name)) in types.into_iter().zip(names) {
            self.methods
                .push((kind.unbind(), serializer.getattr(name)?.unbind()));
        }
        for name in names.map(|(_, name)| name).into_iter().chain([
            "__init__",
            "save",
            "_save",
            "_write_unicode_string",
            "_write_byte_sequence",
            "_save_integral",
            "_write_int4",
            "_write_setitem",
            "_write_set",
        ]) {
            let value = serializer.getattr(name)?;
            if !value.is_exact_instance_of::<PyFunction>() {
                self.initial_admission = Admission::Upstream;
                return Ok(Admission::Upstream);
            }
            let code = value.getattr("__code__")?;
            if !code.getattr("co_filename")?.eq(filename)? {
                self.initial_admission = Admission::Upstream;
            }
            if name == "save" {
                let defaults = value.getattr("__defaults__")?;
                if !defaults.is_exact_instance_of::<PyTuple>()
                    || defaults.cast::<PyTuple>()?.len() != 1
                    || !defaults
                        .cast::<PyTuple>()?
                        .get_item(0)?
                        .is(PyBool::new(py, false))
                {
                    self.initial_admission = Admission::Upstream;
                }
                self.watched.push(WatchedAttribute {
                    owner: value.clone().unbind(),
                    name: "__defaults__",
                    value: defaults.unbind(),
                    code: None,
                });
            }
            self.watched.push(WatchedAttribute {
                owner: serializer.clone().unbind(),
                name,
                value: value.unbind(),
                code: Some(code.unbind()),
            });
        }
        Ok(Admission::Native)
    }

    fn snapshot_transport_globals(
        &mut self,
        namespace: &Bound<'_, PyDict>,
        serializer: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        for name in [
            "_Serializer",
            "opcode",
            "FOUR_BYTE_INT_MAX",
            "FLOAT_FORMAT",
            "COMPLEX_FORMAT",
            "struct",
        ] {
            let value = namespace.get_item(name)?.ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("execnet serializer global is missing")
            })?;
            if (name == "FOUR_BYTE_INT_MAX"
                && (!value.is_exact_instance_of::<PyInt>() || !value.eq(i32::MAX)?))
                || (matches!(name, "FLOAT_FORMAT" | "COMPLEX_FORMAT")
                    && (!value.is_exact_instance_of::<PyString>()
                        || !value.eq(if name == "FLOAT_FORMAT" { "!d" } else { "!dd" })?))
            {
                self.initial_admission = Admission::Upstream;
            }
            self.globals.push((name, value.unbind()));
        }
        let struct_module = namespace.get_item("struct")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("execnet struct module is missing")
        })?;
        for (owner, name) in [
            (serializer.clone(), "__new__"),
            (serializer.clone(), "_dispatch"),
            (struct_module, "pack"),
        ] {
            let value = owner.getattr(name)?;
            if name == "pack"
                && (!value.is_exact_instance_of::<PyCFunction>()
                    || !value.getattr("__module__")?.eq("_struct")?
                    || !value.getattr("__name__")?.eq("pack")?)
            {
                self.initial_admission = Admission::Upstream;
            }
            self.watched.push(WatchedAttribute {
                owner: owner.unbind(),
                name,
                value: value.unbind(),
                code: None,
            });
        }
        Ok(())
    }

    fn snapshot_opcodes(&mut self, namespace: &Bound<'_, PyDict>) -> PyResult<()> {
        let opcodes = namespace.get_item("opcode")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("execnet opcodes are missing")
        })?;
        for (name, opcode) in [
            ("NONE", b'L'),
            ("TRUE", b'R'),
            ("FALSE", b'C'),
            ("INT", b'F'),
            ("LONGINT", b'H'),
            ("PY3STRING", b'N'),
            ("BYTES", b'A'),
            ("FLOAT", b'D'),
            ("COMPLEX", b'T'),
            ("NEWLIST", b'K'),
            ("NEWDICT", b'J'),
            ("SETITEM", b'P'),
            ("BUILDTUPLE", b'@'),
            ("SET", b'O'),
            ("FROZENSET", b'E'),
            ("STOP", b'Q'),
        ] {
            let value = opcodes.getattr(name)?;
            if !value.is_exact_instance_of::<PyBytes>()
                || value.cast::<PyBytes>()?.as_bytes() != [opcode]
            {
                self.initial_admission = Admission::Upstream;
            }
            self.watched.push(WatchedAttribute {
                owner: opcodes.clone().unbind(),
                name,
                value: value.unbind(),
                code: None,
            });
        }
        Ok(())
    }
}

fn length(bytes: &mut Vec<u8>, value: usize) -> PyResult<()> {
    let value = i32::try_from(value)?;
    bytes.extend_from_slice(&value.to_be_bytes());
    Ok(())
}

fn byte_sequence(bytes: &mut Vec<u8>, value: &[u8]) -> PyResult<Admission> {
    if value.len() > i32::MAX as usize {
        return Ok(Admission::Upstream);
    }
    length(bytes, value.len())?;
    bytes.extend_from_slice(value);
    Ok(Admission::Native)
}

fn encode(
    value: &Bound<'_, PyAny>,
    bytes: &mut Vec<u8>,
    depth: usize,
    graph: &mut GraphAdmission,
) -> PyResult<Admission> {
    graph.visits += 1;
    if depth > MAX_DEPTH || graph.visits > MAX_VISITS {
        return Ok(Admission::Upstream);
    }
    match kind(value) {
        Some("NoneType") => bytes.push(b'L'),
        Some("bool") => bytes.push(if value.extract::<bool>()? { b'R' } else { b'C' }),
        Some("int") => {
            if !value.ge(i32::MIN)? {
                return Ok(Admission::Upstream);
            }
            if value.le(i32::MAX)? {
                bytes.push(b'F');
                bytes.extend_from_slice(&value.extract::<i32>()?.to_be_bytes());
            } else {
                bytes.push(b'H');
                return byte_sequence(bytes, value.str()?.to_str()?.as_bytes());
            }
        }
        Some("str") => {
            let text = match value.cast::<PyString>()?.to_str() {
                Ok(text) => text,
                Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(value.py()) => {
                    return Ok(Admission::Upstream);
                }
                Err(error) => return Err(error),
            };
            bytes.push(b'N');
            return byte_sequence(bytes, text.as_bytes());
        }
        Some("bytes") => {
            bytes.push(b'A');
            return byte_sequence(bytes, value.cast::<PyBytes>()?.as_bytes());
        }
        Some("float") => {
            bytes.push(b'D');
            bytes.extend_from_slice(&value.extract::<f64>()?.to_be_bytes());
        }
        Some("complex") => {
            bytes.push(b'T');
            let value = value.cast::<PyComplex>()?;
            bytes.extend_from_slice(&value.real().to_be_bytes());
            bytes.extend_from_slice(&value.imag().to_be_bytes());
        }
        Some(container @ ("list" | "dict" | "tuple" | "set" | "frozenset")) => {
            return encode_container(value, container, bytes, depth, graph);
        }
        _ => return Ok(Admission::Upstream),
    }
    Ok(Admission::Native)
}

fn encode_container(
    value: &Bound<'_, PyAny>,
    container: &str,
    bytes: &mut Vec<u8>,
    depth: usize,
    graph: &mut GraphAdmission,
) -> PyResult<Admission> {
    let count = value.len()?;
    if count > i32::MAX as usize || !graph.ancestors.insert(value.as_ptr() as usize) {
        return Ok(Admission::Upstream);
    }
    match container {
        "list" => {
            bytes.push(b'K');
            length(bytes, count)?;
            for (index, item) in value.cast::<PyList>()?.iter().enumerate() {
                bytes.push(b'F');
                length(bytes, index)?;
                if encode(&item, bytes, depth + 1, graph)? == Admission::Upstream {
                    return Ok(Admission::Upstream);
                }
                bytes.push(b'P');
            }
        }
        "dict" => {
            bytes.push(b'J');
            for (key, item) in value.cast::<PyDict>()? {
                if encode(&key, bytes, depth + 1, graph)? == Admission::Upstream
                    || encode(&item, bytes, depth + 1, graph)? == Admission::Upstream
                {
                    return Ok(Admission::Upstream);
                }
                bytes.push(b'P');
            }
        }
        container => {
            for item in value.try_iter()? {
                if encode(&item?, bytes, depth + 1, graph)? == Admission::Upstream {
                    return Ok(Admission::Upstream);
                }
            }
            bytes.push(match container {
                "tuple" => b'@',
                "set" => b'O',
                _ => b'E',
            });
            length(bytes, count)?;
        }
    }
    graph.ancestors.remove(&(value.as_ptr() as usize));
    Ok(Admission::Native)
}

#[pymethods]
impl NativeExecnetEncoder {
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3's GC protocol owns PyVisit"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.upstream)?;
        visit.call(&self.dispatch)?;
        visit.call(&self.namespace)?;
        for (kind, method) in &self.methods {
            visit.call(kind)?;
            visit.call(method)?;
        }
        for watched in &self.watched {
            visit.call(&watched.owner)?;
            visit.call(&watched.value)?;
            visit.call(&watched.code)?;
        }
        for (_, value) in &self.globals {
            visit.call(value)?;
        }
        Ok(())
    }

    #[new]
    #[pyo3(signature = (upstream = None))]
    fn new(py: Python<'_>, upstream: Option<&Bound<'_, PyAny>>) -> PyResult<Self> {
        let gateway = py.import("execnet.gateway_base")?;
        let upstream = upstream.map_or_else(
            || gateway.getattr("dumps_internal"),
            |value| Ok(value.clone()),
        )?;
        let namespace = upstream.getattr("__globals__")?.cast_into::<PyDict>()?;
        let serializer = namespace.get_item("_Serializer")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("execnet serializer is missing")
        })?;
        if !serializer.is_exact_instance_of::<PyType>()
            || !serializer.getattr("__new__")?.is(py
                .import("builtins")?
                .getattr("object")?
                .getattr("__new__")?)
        {
            return Ok(Self {
                upstream: upstream.unbind(),
                dispatch: PyDict::new(py).unbind(),
                methods: Vec::new(),
                watched: Vec::new(),
                namespace: namespace.unbind(),
                globals: Vec::new(),
                initial_admission: Admission::Upstream,
            });
        }
        let dispatch = serializer.getattr("_dispatch")?.cast_into::<PyDict>()?;
        let initial_admission = if dispatch.is_exact_instance_of::<PyDict>() {
            Admission::Native
        } else {
            Admission::Upstream
        };
        let mut encoder = Self {
            upstream: upstream.clone().unbind(),
            dispatch: dispatch.unbind(),
            methods: Vec::new(),
            watched: Vec::new(),
            namespace: namespace.clone().unbind(),
            globals: Vec::new(),
            initial_admission,
        };
        let filename = upstream.getattr("__code__")?.getattr("co_filename")?;
        encoder.watched.push(WatchedAttribute {
            owner: upstream.clone().unbind(),
            name: "__code__",
            value: upstream.getattr("__code__")?.unbind(),
            code: None,
        });
        if encoder.snapshot_serializer_methods(py, &serializer, &filename)? == Admission::Upstream {
            return Ok(encoder);
        }
        encoder.snapshot_transport_globals(&namespace, &serializer)?;
        encoder.snapshot_opcodes(&namespace)?;
        Ok(encoder)
    }

    fn dumps<'py>(
        &self,
        py: Python<'py>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.transport_admission(py)? == Admission::Upstream {
            return self.upstream.bind(py).call1((value,));
        }
        let mut bytes = Vec::new();
        if encode(
            value,
            &mut bytes,
            0,
            &mut GraphAdmission {
                ancestors: HashSet::new(),
                visits: 0,
            },
        )? == Admission::Upstream
        {
            drop(bytes);
            return self.upstream.bind(py).call1((value,));
        }
        bytes.push(b'Q');
        Ok(PyBytes::new(py, &bytes).into_any())
    }
}
