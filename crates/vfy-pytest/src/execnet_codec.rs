//! Execnet deserialization of the opcode stream produced by the serializer.

use pyo3::exceptions::{PyAssertionError, PyEOFError};
use pyo3::prelude::*;
use pyo3::pyclass::{PyTraverseError, PyVisit};
use pyo3::types::{
    PyBytes, PyComplex, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple,
    PyType,
};

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeExecnetDecoder {
    load_error: Py<PyType>,
    struct_error: Py<PyType>,
    integer: Py<PyAny>,
    singleton_list: Py<PyList>,
    upstream: Py<PyAny>,
}

struct Cursor<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> Cursor<'a> {
    fn read(&mut self, length: i32) -> &'a [u8] {
        let remaining = self.bytes.len() - self.position;
        let length = usize::try_from(length).map_or(remaining, |length| length.min(remaining));
        let start = self.position;
        self.position += length;
        &self.bytes[start..self.position]
    }

    fn integer(&mut self, decoder: &NativeExecnetDecoder, py: Python<'_>) -> PyResult<i32> {
        let bytes: [u8; 4] = self
            .read(4)
            .try_into()
            .map_err(|_| decoder.struct_error(py, 4))?;
        Ok(i32::from_be_bytes(bytes))
    }

    fn byte_string(
        &mut self,
        decoder: &NativeExecnetDecoder,
        py: Python<'_>,
    ) -> PyResult<&'a [u8]> {
        let length = self.integer(decoder, py)?;
        Ok(self.read(length))
    }

    fn float(&mut self, decoder: &NativeExecnetDecoder, py: Python<'_>) -> PyResult<f64> {
        let bytes: [u8; 8] = self
            .read(8)
            .try_into()
            .map_err(|_| decoder.struct_error(py, 8))?;
        Ok(f64::from_be_bytes(bytes))
    }
}

impl NativeExecnetDecoder {
    fn native_packet(data: &[u8]) -> bool {
        let mut cursor = Cursor {
            bytes: data,
            position: 0,
        };
        let mut depth = 0_usize;
        while let Some(opcode) = cursor.read(1).first().copied() {
            let operand_size = match opcode {
                b'F' | b'G' => 4,
                b'D' => 8,
                b'T' => 16,
                _ => 0,
            };
            if operand_size > 0 {
                if cursor.read(operand_size).len() != usize::try_from(operand_size).unwrap_or(0) {
                    return false;
                }
                depth += 1;
                continue;
            }
            match opcode {
                b'L' | b'R' | b'C' | b'J' => depth += 1,
                b'A' | b'M' | b'N' | b'S' | b'H' | b'I' | b'K' | b'@' | b'O' | b'E' => {
                    let Ok(bytes) = <[u8; 4]>::try_from(cursor.read(4)) else {
                        return false;
                    };
                    let length = i32::from_be_bytes(bytes);
                    match opcode {
                        b'K' => depth += 1,
                        b'@' | b'O' | b'E' => {
                            let Ok(count) = usize::try_from(length.unsigned_abs()) else {
                                return false;
                            };
                            depth = if length >= 0 {
                                depth.saturating_sub(count)
                            } else {
                                count.min(depth)
                            } + 1;
                        }
                        _ => {
                            cursor.read(length);
                            depth += 1;
                        }
                    }
                }
                b'P' => {
                    if depth < 3 {
                        return false;
                    }
                    depth -= 2;
                }
                b'Q' => return depth == 1,
                _ => return false,
            }
        }
        false
    }

    fn load_error(&self, py: Python<'_>, message: String) -> PyErr {
        PyErr::from_type(self.load_error.bind(py).clone(), (message,))
    }

    fn struct_error(&self, py: Python<'_>, size: usize) -> PyErr {
        PyErr::from_type(
            self.struct_error.bind(py).clone(),
            (format!("unpack requires a buffer of {size} bytes"),),
        )
    }
}

#[pymethods]
impl NativeExecnetDecoder {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Self> {
        Ok(Self {
            load_error: py
                .import("execnet.gateway_base")?
                .getattr("LoadError")?
                .cast_into::<PyType>()?
                .unbind(),
            struct_error: py
                .import("struct")?
                .getattr("error")?
                .cast_into::<PyType>()?
                .unbind(),
            integer: py.import("builtins")?.getattr("int")?.unbind(),
            singleton_list: PyList::new(py, [py.None()])?.unbind(),
            upstream: py
                .import("execnet.gateway_base")?
                .getattr("loads_internal")?
                .unbind(),
        })
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3's GC protocol owns PyVisit"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.load_error)?;
        visit.call(&self.struct_error)?;
        visit.call(&self.integer)?;
        visit.call(&self.singleton_list)?;
        visit.call(&self.upstream)?;
        Ok(())
    }

    #[pyo3(
        signature = (data, strconfig = (true, false), channel_or_gateway = None),
        text_signature = "($self, /, data, strconfig=(True, False), channel_or_gateway=None)"
    )]
    #[expect(
        clippy::too_many_lines,
        reason = "execnet opcodes share one byte cursor and value stack"
    )]
    fn loads<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
        strconfig: (bool, bool),
        channel_or_gateway: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Channels and malformed framing retain execnet's callback and traceback ownership.
        if !Self::native_packet(data) {
            return self.upstream.bind(py).call1((
                PyBytes::new(py, data),
                channel_or_gateway,
                strconfig,
            ));
        }
        let strconfig = match channel_or_gateway {
            Some(channel) => match channel.getattr("_strconfig")?.extract::<(bool, bool)>() {
                Ok(configuration) => configuration,
                Err(_) => {
                    return self.upstream.bind(py).call1((
                        PyBytes::new(py, data),
                        channel_or_gateway,
                        strconfig,
                    ));
                }
            },
            None => strconfig,
        };
        let mut cursor = Cursor {
            bytes: data,
            position: 0,
        };
        let mut stack: Vec<Bound<'py, PyAny>> = Vec::new();
        loop {
            let Some(opcode) = cursor.read(1).first().copied() else {
                return Err(PyEOFError::new_err(()));
            };
            let value = match opcode {
                b'L' => py.None().into_bound(py),
                b'R' => true.into_pyobject(py)?.to_owned().into_any(),
                b'C' => false.into_pyobject(py)?.to_owned().into_any(),
                b'F' | b'G' => PyInt::new(py, cursor.integer(self, py)?).into_any(),
                b'H' | b'I' => self
                    .integer
                    .bind(py)
                    .call1((PyBytes::new(py, cursor.byte_string(self, py)?),))?,
                b'D' => PyFloat::new(py, cursor.float(self, py)?).into_any(),
                b'T' => {
                    // struct.unpack reads both components before constructing complex.
                    let bytes: [u8; 16] = cursor
                        .read(16)
                        .try_into()
                        .map_err(|_| self.struct_error(py, 16))?;
                    let mut parts = Cursor {
                        bytes: &bytes,
                        position: 0,
                    };
                    PyComplex::from_doubles(py, parts.float(self, py)?, parts.float(self, py)?)
                        .into_any()
                }
                b'A' | b'M' | b'N' | b'S' => {
                    let bytes = PyBytes::new(py, cursor.byte_string(self, py)?);
                    let encoding = match opcode {
                        b'M' if strconfig.0 => Some(c"latin-1"),
                        b'N' if !strconfig.1 => Some(c"utf-8"),
                        b'S' => Some(c"utf-8"),
                        _ => None,
                    };
                    match encoding {
                        Some(encoding) => {
                            PyString::from_encoded_object(bytes.as_any(), Some(encoding), None)?
                                .into_any()
                        }
                        None => bytes.into_any(),
                    }
                }
                b'J' => PyDict::new(py).into_any(),
                b'K' => {
                    let length = cursor.integer(self, py)?;
                    self.singleton_list.bind(py).as_any().mul(length)?
                }
                b'P' => {
                    if stack.len() < 3 {
                        return Err(self.load_error(py, "not enough items for setitem".to_owned()));
                    }
                    let value = stack.pop().ok_or_else(|| {
                        self.load_error(py, "not enough items for setitem".to_owned())
                    })?;
                    let key = stack.pop().ok_or_else(|| {
                        self.load_error(py, "not enough items for setitem".to_owned())
                    })?;
                    stack[stack.len() - 1].set_item(key, value)?;
                    continue;
                }
                b'@' | b'O' | b'E' => {
                    let length = cursor.integer(self, py)?;
                    let start = if length >= 0 {
                        stack.len().saturating_sub(
                            usize::try_from(length).map_err(|_| PyAssertionError::new_err(()))?,
                        )
                    } else {
                        usize::try_from(-i64::from(length))
                            .map_err(|_| PyAssertionError::new_err(()))?
                            .min(stack.len())
                    };
                    let values = stack.split_off(start);
                    match opcode {
                        b'@' => PyTuple::new(py, values)?.into_any(),
                        b'O' => PySet::new(py, &values)?.into_any(),
                        _ => PyFrozenSet::new(py, &values)?.into_any(),
                    }
                }
                b'B' => {
                    return Err(self.load_error(
                        py,
                        "channel opcode passed native framing validation".to_owned(),
                    ));
                }
                b'Q' => {
                    if stack.len() != 1 {
                        return Err(
                            self.load_error(py, "internal unserialization error".to_owned())
                        );
                    }
                    return stack.pop().ok_or_else(|| {
                        self.load_error(py, "internal unserialization error".to_owned())
                    });
                }
                _ => {
                    let representation = PyBytes::new(py, &[opcode]).repr()?.to_string();
                    return Err(self.load_error(
                        py,
                        format!("unknown opcode {representation} - wire protocol corruption?"),
                    ));
                }
            };
            stack.push(value);
        }
    }
}
