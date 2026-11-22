//! Shared callspec payloads with exact, mutable Python containers on observation.

use std::collections::HashMap;

use pyo3::exceptions::{PyIndexError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::{PyTraverseError, PyVisit};
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

pub(crate) type RowParameterOwners = Vec<(u32, Py<PyAny>)>;

const FIELDS: [(&str, Field); 5] = [
    ("params", Field::Params),
    ("indices", Field::Indices),
    ("_arg2scope", Field::Scopes),
    ("_idlist", Field::Ids),
    ("marks", Field::Marks),
];

#[derive(Clone, Copy, Eq, Hash, PartialEq)]
enum Field {
    Params,
    Indices,
    Scopes,
    Ids,
    Marks,
}

impl Field {
    fn parse(name: &str) -> PyResult<Self> {
        match name {
            "params" => Ok(Self::Params),
            "indices" => Ok(Self::Indices),
            "_arg2scope" => Ok(Self::Scopes),
            "_idlist" => Ok(Self::Ids),
            "marks" => Ok(Self::Marks),
            _ => Err(PyValueError::new_err(format!(
                "unknown callspec field {name:?}"
            ))),
        }
    }
}

#[derive(Clone, Copy)]
struct Slice {
    start: u32,
    len: u32,
}

impl Slice {
    fn range(self) -> std::ops::Range<usize> {
        self.start as usize..(self.start as usize + self.len as usize)
    }
}

#[derive(Clone, Copy)]
struct Mapping {
    shape: u32,
    values: Slice,
}

struct Row {
    params: Mapping,
    indices: Mapping,
    scopes: Mapping,
    ids: Slice,
    marks: Slice,
    ids_tuple: bool,
    marks_tuple: bool,
    active: bool,
    promoted: u8,
}

impl Row {
    fn mapping(&self, field: Field) -> Option<Mapping> {
        match field {
            Field::Params => Some(self.params),
            Field::Indices => Some(self.indices),
            Field::Scopes => Some(self.scopes),
            Field::Ids | Field::Marks => None,
        }
    }

    fn values(&self, field: Field) -> Slice {
        match field {
            Field::Params => self.params.values,
            Field::Indices => self.indices.values,
            Field::Scopes => self.scopes.values,
            Field::Ids => self.ids,
            Field::Marks => self.marks,
        }
    }
}

struct Object {
    // Values and marks can be arbitrary Python objects. A promoted field takes
    // ownership through its real container; the arena then releases its uses so
    // later dictionary/list edits preserve destructor and weakref timing.
    value: Option<Py<PyAny>>,
    uses: usize,
}

#[derive(Default)]
struct Builder {
    objects: Vec<Object>,
    identities: HashMap<usize, u32>,
    shapes: Vec<Vec<u32>>,
    shape_ids: HashMap<Vec<u32>, u32>,
    cells: Vec<u32>,
}

fn compact(value: usize) -> PyResult<u32> {
    u32::try_from(value)
        .map_err(|_| PyValueError::new_err("callspec family exceeds dense storage limit"))
}

impl Builder {
    fn shared_mapping<'py>(
        &mut self,
        shape: u32,
        values: impl Iterator<Item = PyResult<Bound<'py, PyAny>>>,
    ) -> PyResult<Mapping> {
        for key in &self.shapes[shape as usize] {
            self.objects[*key as usize].uses += 1;
        }
        let start = compact(self.cells.len())?;
        let mut len = 0;
        for value in values {
            let value = self.object(&value?)?;
            self.cells.push(value);
            len += 1;
        }
        if len != self.shapes[shape as usize].len() {
            return Err(PyValueError::new_err("parameter row has incorrect width"));
        }
        Ok(Mapping {
            shape,
            values: Slice {
                start,
                len: compact(len)?,
            },
        })
    }

    fn object(&mut self, value: &Bound<'_, PyAny>) -> PyResult<u32> {
        let identity = value.as_ptr() as usize;
        if let Some(&index) = self.identities.get(&identity) {
            self.objects[index as usize].uses += 1;
            return Ok(index);
        }
        let index = compact(self.objects.len())?;
        self.objects.push(Object {
            value: Some(value.clone().unbind()),
            uses: 1,
        });
        self.identities.insert(identity, index);
        Ok(index)
    }

    fn mapping(&mut self, value: &Bound<'_, PyAny>) -> PyResult<Mapping> {
        let mapping = value.cast_exact::<PyDict>()?;
        let start = compact(self.cells.len())?;
        let mut shape = Vec::with_capacity(mapping.len());
        for (key, value) in mapping {
            key.cast_exact::<PyString>()?;
            shape.push(self.object(&key)?);
            let cell = self.object(&value)?;
            self.cells.push(cell);
        }
        let len = compact(mapping.len())?;
        let shape_id = if let Some(&index) = self.shape_ids.get(&shape) {
            index
        } else {
            let index = compact(self.shapes.len())?;
            self.shapes.push(shape.clone());
            self.shape_ids.insert(shape, index);
            index
        };
        Ok(Mapping {
            shape: shape_id,
            values: Slice { start, len },
        })
    }

    fn sequence(&mut self, value: &Bound<'_, PyAny>, strings: bool) -> PyResult<(Slice, bool)> {
        let tuple = value.is_exact_instance_of::<PyTuple>();
        if !tuple && !value.is_exact_instance_of::<PyList>() {
            return Err(PyTypeError::new_err(
                "dense callspec sequences require exact list or tuple",
            ));
        }
        let start = compact(self.cells.len())?;
        let len = compact(value.len()?)?;
        for member in value.try_iter()? {
            let member = member?;
            if strings {
                member.cast_exact::<PyString>()?;
            }
            let cell = self.object(&member)?;
            self.cells.push(cell);
        }
        Ok((Slice { start, len }, tuple))
    }
}

/// The adapter retains exact `CallSpec2` identity; this owner retains only payloads.
#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct DenseCallSpecFamily {
    objects: Vec<Object>,
    shapes: Vec<Vec<u32>>,
    cells: Vec<u32>,
    rows: Vec<Row>,
    observed: HashMap<(usize, Field), Py<PyAny>>,
    function_scope: Option<Py<PyAny>>,
    anchored: bool,
}

pub(crate) struct PackedCallSpecs<'py> {
    pub(crate) family: DenseCallSpecFamily,
    pub(crate) callspecs: Bound<'py, PyList>,
    pub(crate) raw_rows: Vec<Bound<'py, PyDict>>,
}

fn eligible(raw: &Bound<'_, PyDict>) -> PyResult<bool> {
    if raw.contains("_vfy_dense_row")? {
        return Ok(false);
    }
    if raw
        .iter()
        .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
    {
        return Ok(false);
    }
    for (name, field) in FIELDS {
        let Some(value) = raw.get_item(name)? else {
            return Ok(false);
        };
        if field == Field::Ids || field == Field::Marks {
            if !value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyTuple>() {
                return Ok(false);
            }
            if field == Field::Ids {
                for member in value.try_iter()? {
                    if !member?.is_exact_instance_of::<PyString>() {
                        return Ok(false);
                    }
                }
            }
        } else {
            let Ok(mapping) = value.cast_exact::<PyDict>() else {
                return Ok(false);
            };
            if mapping
                .iter()
                .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
            {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

#[derive(Clone, Copy)]
pub(crate) enum FirstTableFormat {
    Numeric,
    TupleRows,
}

impl DenseCallSpecFamily {
    /// Keep the initial constructor batch to unobserved zero/single-part IDs.
    /// The original constructor handles products and public container aliases.
    pub(crate) fn eager_case(
        &self,
        py: Python<'_>,
        row: usize,
        callspec: Py<PyAny>,
    ) -> PyResult<Option<crate::function_batch::Case>> {
        let value = self.row(row)?;
        if value.promoted & (field_bit(Field::Ids) | field_bit(Field::Marks)) != 0
            || value.ids.len != 1
            || self.observed.contains_key(&(row, Field::Ids))
            || self.observed.contains_key(&(row, Field::Marks))
        {
            return Ok(None);
        }
        let identifier = self
            .object(self.cells[value.ids.start as usize])?
            .clone_ref(py);
        if !identifier.bind(py).is_exact_instance_of::<PyString>() {
            return Ok(None);
        }
        let markers = self.cells[value.marks.range()]
            .iter()
            .map(|index| self.object(*index).map(|value| value.clone_ref(py)))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Some(crate::function_batch::Case {
            callspec,
            identifier,
            has_ids: value.ids.len != 0,
            markers,
        }))
    }

    pub(crate) fn row_count(&self) -> usize {
        self.rows.len()
    }

    /// Build the existing dense model before allocating any public payload maps.
    pub(crate) fn first_table(
        py: Python<'_>,
        argnames: &Bound<'_, PyAny>,
        values: &[Bound<'_, PyAny>],
        ids: &Bound<'_, PyList>,
        format: FirstTableFormat,
        scope: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        if values.len() != ids.len() {
            return Err(PyValueError::new_err("parameter row and ID counts differ"));
        }
        let mut builder = Builder::default();
        let names = argnames.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        let mut shape = Vec::with_capacity(names.len());
        for name in &names {
            name.cast_exact::<PyString>()?;
            let index = builder.object(name)?;
            builder.objects[index as usize].uses -= 1;
            shape.push(index);
        }
        builder.shapes.push(shape);
        let mut rows = Vec::with_capacity(values.len());
        for (index, (value, id)) in values.iter().zip(ids.iter()).enumerate() {
            let params = match format {
                FirstTableFormat::Numeric => {
                    builder.shared_mapping(0, std::iter::once(Ok(value.clone())))?
                }
                FirstTableFormat::TupleRows => {
                    builder.shared_mapping(0, value.cast_exact::<PyTuple>()?.iter().map(Ok))?
                }
            };
            let index = index.into_pyobject(py)?.into_any();
            let indices = builder.shared_mapping(0, names.iter().map(|_| Ok(index.clone())))?;
            let scopes = builder.shared_mapping(0, names.iter().map(|_| Ok(scope.clone())))?;
            id.cast_exact::<PyString>()?;
            let start = compact(builder.cells.len())?;
            let id = builder.object(&id)?;
            builder.cells.push(id);
            let ids = Slice { start, len: 1 };
            let marks = Slice {
                start: compact(builder.cells.len())?,
                len: 0,
            };
            rows.push(Row {
                params,
                indices,
                scopes,
                ids,
                marks,
                ids_tuple: false,
                marks_tuple: false,
                active: true,
                promoted: 0,
            });
        }
        Ok(Self {
            objects: builder.objects,
            shapes: builder.shapes,
            cells: builder.cells,
            rows,
            observed: HashMap::new(),
            function_scope: Some(scope.clone().unbind()),
            anchored: false,
        })
    }

    /// Check the complete recompute batch before any native index changes.
    pub(crate) fn direct_indices_admitted(
        &self,
        py: Python<'_>,
        row: usize,
        names: &[Bound<'_, PyAny>],
    ) -> PyResult<bool> {
        let row_data = self.row(row)?;
        if row_data.promoted & field_bit(Field::Indices) != 0
            || self.observed.contains_key(&(row, Field::Indices))
        {
            return Ok(false);
        }
        let shape = &self.shapes[row_data.indices.shape as usize];
        for name in names {
            let mut found = false;
            for key in shape {
                if self.object(*key)?.bind(py).eq(name)? {
                    found = true;
                    break;
                }
            }
            if !found {
                return Ok(false);
            }
        }
        for value in &self.cells[row_data.indices.values.range()] {
            if !self
                .object(*value)?
                .bind(py)
                .is_exact_instance_of::<pyo3::types::PyInt>()
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Only admitted exact integer cells change; drop displaced refs after borrow.
    pub(crate) fn set_direct_index(
        family: &Py<Self>,
        py: Python<'_>,
        row: usize,
        name: &Bound<'_, PyAny>,
        index: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let removed = {
            let mut state = family.borrow_mut(py);
            let mapping = state.row(row)?.indices;
            let mut column = None;
            for (offset, key) in state.shapes[mapping.shape as usize].iter().enumerate() {
                if state.object(*key)?.bind(py).eq(name)? {
                    column = Some(offset);
                    break;
                }
            }
            let column =
                column.ok_or_else(|| PyValueError::new_err("direct parameter disappeared"))?;
            let cell = mapping.values.start as usize + column;
            let old = state.cells[cell] as usize;
            if state.objects[old].uses == 1 {
                state.objects[old].value.replace(index.clone().unbind())
            } else {
                let next = compact(state.objects.len())?;
                state.objects.push(Object {
                    value: Some(index.clone().unbind()),
                    uses: 1,
                });
                state.objects[old].uses -= 1;
                state.cells[cell] = next;
                None
            }
        };
        drop(removed);
        Ok(())
    }

    pub(crate) fn prepare_packed<'py>(
        py: Python<'py>,
        callspecs: &Bound<'py, PyList>,
        constructor: &Bound<'py, PyType>,
        raw_descriptor: &Bound<'py, PyAny>,
    ) -> PyResult<Option<PackedCallSpecs<'py>>> {
        let mut packed = Vec::new();
        let mut raw_rows = Vec::new();
        for callspec in callspecs {
            if !callspec.is_exact_instance(constructor) {
                continue;
            }
            let raw = raw_descriptor
                .call_method1("__get__", (&callspec,))?
                .cast_into::<PyDict>()?;
            // Unlike sys.getrefcount, this reads without creating an argument
            // reference: the instance and this Bound are the two ordinary owners.
            #[expect(deprecated, reason = "PyO3's replacement requires unsafe code.")]
            let unobserved = raw.get_refcnt() == 2;
            if !unobserved || !eligible(&raw)? {
                continue;
            }
            packed.push(callspec);
            raw_rows.push(raw);
        }
        if packed.is_empty() {
            return Ok(None);
        }
        let family = Self::from_raw(py, raw_rows.iter().cloned().map(Ok))?;
        Ok(Some(PackedCallSpecs {
            family,
            callspecs: PyList::new(py, packed)?,
            raw_rows,
        }))
    }

    pub(crate) fn strip_packed(
        py: Python<'_>,
        family: &Py<Self>,
        raw_rows: &[Bound<'_, PyDict>],
    ) -> PyResult<()> {
        for (row, raw) in raw_rows.iter().enumerate() {
            for (name, field) in FIELDS {
                let state = family.borrow(py);
                let retained = state.observed.contains_key(&(row, field))
                    || state.rows[row].promoted & field_bit(field) != 0;
                drop(state);
                if !retained {
                    // A dictionary edit can run Python. Release the arena borrow
                    // first so callbacks can observe or replace callspec fields.
                    raw.del_item(name)?;
                }
            }
        }
        Ok(())
    }

    fn from_raw<'py>(
        py: Python<'py>,
        raw_rows: impl Iterator<Item = PyResult<Bound<'py, PyDict>>>,
    ) -> PyResult<Self> {
        let function_scope = py
            .import("_pytest.scope")?
            .getattr("Scope")?
            .getattr("Function")?
            .unbind();
        let mut builder = Builder::default();
        let mut rows = Vec::new();
        let mut retained = HashMap::new();
        for raw in raw_rows {
            let raw = raw?;
            let mut read = |name: &str, field| -> PyResult<Bound<'_, PyAny>> {
                let value = raw
                    .get_item(name)?
                    .ok_or_else(|| PyValueError::new_err(format!("callspec has no {name}")))?;
                // The raw dictionary and this Bound are the two ordinary owners.
                // Additional owners include earlier observers and shared fields;
                // retain their exact container rather than severing those aliases.
                #[expect(deprecated, reason = "PyO3's replacement requires unsafe code.")]
                let retained_alias = value.get_refcnt() > 2;
                if retained_alias {
                    retained.insert((rows.len(), field), value.clone().unbind());
                }
                Ok(value)
            };
            let params = builder.mapping(&read("params", Field::Params)?)?;
            let indices = builder.mapping(&read("indices", Field::Indices)?)?;
            let scopes = builder.mapping(&read("_arg2scope", Field::Scopes)?)?;
            let (ids, ids_tuple) = builder.sequence(&read("_idlist", Field::Ids)?, true)?;
            let (marks, marks_tuple) = builder.sequence(&read("marks", Field::Marks)?, false)?;
            rows.push(Row {
                params,
                indices,
                scopes,
                ids,
                marks,
                ids_tuple,
                marks_tuple,
                active: true,
                promoted: 0,
            });
        }
        let mut family = Self {
            objects: builder.objects,
            shapes: builder.shapes,
            cells: builder.cells,
            rows,
            observed: HashMap::new(),
            function_scope: Some(function_scope),
            anchored: false,
        };
        for ((row, field), value) in retained {
            family.observed.insert((row, field), value);
            family.release(row, field)?;
        }
        Ok(family)
    }

    fn untouched_function_scoped(
        &self,
        py: Python<'_>,
        row_data: &Row,
        function_scope: Option<&Py<PyAny>>,
    ) -> PyResult<bool> {
        let names = &self.shapes[row_data.indices.shape as usize];
        if names.is_empty() {
            return Ok(true);
        }
        let function = function_scope
            .or(self.function_scope.as_ref())
            .ok_or_else(|| PyValueError::new_err("callspec family was cleared"))?
            .bind(py);
        let scopes = &self.cells[row_data.scopes.values.range()];
        // Ordinary rows share the same ordered argument-name shape in both
        // dictionaries. Check their scope references without allocating any
        // per-case containers or temporary reference vectors.
        if row_data.indices.shape == row_data.scopes.shape {
            for scope in scopes {
                if !self.object(*scope)?.bind(py).is(function) {
                    return Ok(false);
                }
            }
            return Ok(true);
        }
        let scope_names = &self.shapes[row_data.scopes.shape as usize];
        for name in names {
            let name = self.object(*name)?.bind(py);
            let mut found = false;
            for (scope_name, scope) in scope_names.iter().zip(scopes) {
                // Construction validates exact strings, so equality cannot
                // invoke user code while this arena borrow is held.
                if name.eq(self.object(*scope_name)?.bind(py))? {
                    if !self.object(*scope)?.bind(py).is(function) {
                        return Ok(false);
                    }
                    found = true;
                    break;
                }
            }
            if !found {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn row(&self, index: usize) -> PyResult<&Row> {
        let row = self
            .rows
            .get(index)
            .ok_or_else(|| PyIndexError::new_err("callspec row index out of range"))?;
        if !row.active {
            return Err(PyValueError::new_err("callspec row was detached"));
        }
        Ok(row)
    }

    fn object(&self, index: u32) -> PyResult<&Py<PyAny>> {
        self.objects
            .get(index as usize)
            .and_then(|object| object.value.as_ref())
            .ok_or_else(|| PyValueError::new_err("callspec field was already promoted"))
    }

    fn release(&mut self, row: usize, field: Field) -> PyResult<Vec<Py<PyAny>>> {
        let row = self.row(row)?;
        let values = row.values(field);
        let keys = row
            .mapping(field)
            .map(|mapping| self.shapes[mapping.shape as usize].as_slice())
            .unwrap_or_default();
        let mut released = Vec::new();
        for index in keys.iter().chain(&self.cells[values.range()]) {
            let object = &mut self.objects[*index as usize];
            object.uses -= 1;
            if object.uses == 0
                && let Some(value) = object.value.take()
            {
                released.push(value);
            }
        }
        Ok(released)
    }

    fn snapshot(&self, py: Python<'_>, row: usize, field: Field) -> PyResult<Payload> {
        self.snapshot_owned(py, row, field, &[])
    }
    fn resolve<'a>(&'a self, index: u32, owned: &'a [(u32, Py<PyAny>)]) -> PyResult<&'a Py<PyAny>> {
        self.objects
            .get(index as usize)
            .and_then(|object| object.value.as_ref())
            .or_else(|| {
                owned
                    .iter()
                    .find(|(key, _)| *key == index)
                    .map(|(_, value)| value)
            })
            .ok_or_else(|| PyValueError::new_err("callspec object is no longer owned by this row"))
    }
    fn snapshot_owned(
        &self,
        py: Python<'_>,
        row: usize,
        field: Field,
        owned: &[(u32, Py<PyAny>)],
    ) -> PyResult<Payload> {
        let row = self.row(row)?;
        let values = &self.cells[row.values(field).range()];
        if let Some(mapping) = row.mapping(field) {
            return Ok(Payload::Mapping(
                self.shapes[mapping.shape as usize]
                    .iter()
                    .zip(values)
                    .map(|(key, value)| {
                        Ok((
                            self.resolve(*key, owned)?.clone_ref(py),
                            self.resolve(*value, owned)?.clone_ref(py),
                        ))
                    })
                    .collect::<PyResult<Vec<_>>>()?,
            ));
        }
        let members = values
            .iter()
            .map(|value| self.resolve(*value, owned).map(|value| value.clone_ref(py)))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Payload::Sequence(
            members,
            match field {
                Field::Ids => row.ids_tuple,
                Field::Marks => row.marks_tuple,
                _ => false,
            },
        ))
    }

    /// Admission is closed over every strong arena object; scopes are captured
    /// enum singletons already owned by the pytest Scope class.
    pub(crate) fn admit_anchor(
        &mut self,
        py: Python<'_>,
        scopes: &[Bound<'_, PyAny>],
        row_local_params: bool,
    ) -> bool {
        if !self.observed.is_empty()
            || self
                .rows
                .iter()
                .any(|row| row.marks.len != 0 || row.ids.len == 0)
        {
            return false;
        }
        let shared = |value: &Bound<'_, PyAny>| {
            scopes.iter().any(|scope| value.is(scope)) || primitive(value, 0)
        };
        if !row_local_params
            && self.objects.iter().any(|object| {
                object
                    .value
                    .as_ref()
                    .is_some_and(|value| !shared(value.bind(py)))
            })
        {
            return false;
        }
        // Only params values may use row-local opaque ownership. Every other
        // arena edge remains an immutable primitive or captured scope singleton.
        for row in &self.rows {
            for field in [
                Field::Params,
                Field::Indices,
                Field::Scopes,
                Field::Ids,
                Field::Marks,
            ] {
                if let Some(mapping) = row.mapping(field)
                    && self.shapes[mapping.shape as usize]
                        .iter()
                        .any(|key| self.object(*key).is_ok_and(|value| !shared(value.bind(py))))
                {
                    return false;
                }
                if field != Field::Params
                    && self.cells[row.values(field).range()].iter().any(|index| {
                        self.object(*index)
                            .is_ok_and(|value| !shared(value.bind(py)))
                    })
                {
                    return false;
                }
            }
        }
        self.anchored = true;
        true
    }

    /// Transfer every opaque arena reference to the corresponding row owner.
    /// Returning removed arena owners keeps decrefs outside the family borrow.
    pub(crate) fn take_row_parameters(
        &mut self,
        py: Python<'_>,
        scopes: &[Bound<'_, PyAny>],
    ) -> (Option<Vec<RowParameterOwners>>, Vec<Py<PyAny>>) {
        let opaque = self
            .objects
            .iter()
            .map(|object| {
                object.value.as_ref().is_some_and(|value| {
                    let value = value.bind(py);
                    !scopes.iter().any(|scope| value.is(scope)) && !primitive(value, 0)
                })
            })
            .collect::<Vec<_>>();
        if !opaque.iter().any(|value| *value) {
            return (None, Vec::new());
        }
        let mut owners = Vec::with_capacity(self.rows.len());
        for row in &self.rows {
            let mut values = Vec::new();
            for index in &self.cells[row.params.values.range()] {
                if opaque[*index as usize]
                    && !values.iter().any(|(prior, _)| prior == index)
                    && let Some(value) = &self.objects[*index as usize].value
                {
                    values.push((*index, value.clone_ref(py)));
                }
            }
            owners.push(values);
        }
        let mut removed = Vec::new();
        for (index, opaque) in opaque.into_iter().enumerate() {
            if opaque && let Some(value) = self.objects[index].value.take() {
                removed.push(value);
            }
        }
        (Some(owners), removed)
    }

    /// Recheck the original containers after all GC-capable header/weakref
    /// allocations. Identity comparisons neither invoke Python nor allocate.
    pub(crate) fn anchor_ready(&self) -> bool {
        self.anchored && self.rows.iter().all(|row| row.active && row.promoted == 0)
    }

    pub(crate) fn anchor_matches_raw(&self, row: usize, raw: &Bound<'_, PyDict>) -> PyResult<bool> {
        let row_data = self.row(row)?;
        for (name, field) in FIELDS {
            let Some(value) = raw.get_item(name)? else {
                return Ok(false);
            };
            // The raw dict and this Bound are the only admitted container owners.
            #[expect(
                deprecated,
                reason = "Read-only CPython admission; PyO3's replacement requires unsafe code."
            )]
            if value.get_refcnt() != 2 {
                return Ok(false);
            }
            let cells = &self.cells[row_data.values(field).range()];
            if let Some(mapping) = row_data.mapping(field) {
                let Ok(value) = value.cast_exact::<PyDict>() else {
                    return Ok(false);
                };
                if value.len() != cells.len() {
                    return Ok(false);
                }
                for (((key, value), name), cell) in value
                    .iter()
                    .zip(&self.shapes[mapping.shape as usize])
                    .zip(cells)
                {
                    if !key.is(self.object(*name)?.bind(raw.py()))
                        || !value.is(self.object(*cell)?.bind(raw.py()))
                    {
                        return Ok(false);
                    }
                }
            } else {
                let tuple = match field {
                    Field::Ids => row_data.ids_tuple,
                    Field::Marks => row_data.marks_tuple,
                    _ => false,
                };
                if tuple {
                    let Ok(value) = value.cast_exact::<PyTuple>() else {
                        return Ok(false);
                    };
                    if value.len() != cells.len()
                        || value.iter().zip(cells).any(|(value, cell)| {
                            self.object(*cell)
                                .is_ok_and(|original| !value.is(original.bind(raw.py())))
                        })
                    {
                        return Ok(false);
                    }
                } else {
                    let Ok(value) = value.cast_exact::<PyList>() else {
                        return Ok(false);
                    };
                    if value.len() != cells.len()
                        || value.iter().zip(cells).any(|(value, cell)| {
                            self.object(*cell)
                                .is_ok_and(|original| !value.is(original.bind(raw.py())))
                        })
                    {
                        return Ok(false);
                    }
                }
            }
        }
        Ok(true)
    }

    pub(crate) fn anchor_snapshot(
        &self,
        py: Python<'_>,
        row: usize,
        name: &str,
        owned: &[(u32, Py<PyAny>)],
    ) -> PyResult<Payload> {
        if !self.anchored {
            return Err(PyValueError::new_err(
                "row does not have primitive anchor ownership",
            ));
        }
        let field = Field::parse(name)?;
        if self.row(row)?.promoted & field_bit(field) != 0 {
            return Err(PyValueError::new_err(
                "callspec field has already been handed to its header",
            ));
        }
        self.snapshot_owned(py, row, field, owned)
    }

    pub(crate) fn anchor_mapping_available(&self, row: usize, name: &str) -> PyResult<bool> {
        let field = Field::parse(name)?;
        Ok(self.anchored
            && self
                .rows
                .get(row)
                .is_some_and(|row| row.active && row.promoted & field_bit(field) == 0))
    }

    pub(crate) fn anchor_mapping_item(
        &self,
        py: Python<'_>,
        row: usize,
        name: &str,
        key: &str,
        owned: &[(u32, Py<PyAny>)],
    ) -> PyResult<Option<Py<PyAny>>> {
        let field = Field::parse(name)?;
        let row_data = self.row(row)?;
        if row_data.promoted & field_bit(field) != 0 {
            return Err(PyValueError::new_err(
                "callspec mapping is owned by its header",
            ));
        }
        let mapping = row_data
            .mapping(field)
            .ok_or_else(|| PyValueError::new_err("callspec field is not a mapping"))?;
        for (name, value) in self.shapes[mapping.shape as usize]
            .iter()
            .zip(&self.cells[mapping.values.range()])
        {
            if self.object(*name)?.bind(py).eq(key)? {
                return Ok(Some(self.resolve(*value, owned)?.clone_ref(py)));
            }
        }
        Ok(None)
    }

    pub(crate) fn anchor_forget(&mut self, row: usize, name: &str) -> PyResult<Vec<Py<PyAny>>> {
        let field = Field::parse(name)?;
        if self
            .rows
            .get(row)
            .is_none_or(|row| !row.active || row.promoted & field_bit(field) != 0)
        {
            return Ok(Vec::new());
        }
        let released = self.release(row, field)?;
        self.rows[row].promoted |= field_bit(field);
        Ok(released)
    }

    pub(crate) fn detach_if_active(&mut self, row: usize) -> PyResult<Vec<Py<PyAny>>> {
        if self.rows.get(row).is_none_or(|row| !row.active) {
            return Ok(Vec::new());
        }
        self.detach(row)
    }
}

fn field_bit(field: Field) -> u8 {
    1 << (field as u8)
}

fn primitive(value: &Bound<'_, PyAny>, depth: usize) -> bool {
    use pyo3::types::{PyBool, PyBytes, PyComplex, PyFloat, PyInt};
    if value.is_none()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyComplex>()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyBytes>()
    {
        return true;
    }
    depth < 64
        && value
            .cast_exact::<PyTuple>()
            .is_ok_and(|tuple| tuple.iter().all(|member| primitive(&member, depth + 1)))
}

pub(crate) enum Payload {
    Mapping(Vec<(Py<PyAny>, Py<PyAny>)>),
    Sequence(Vec<Py<PyAny>>, bool),
}
impl Payload {
    /// The arena borrow ended before any tracked Python object is allocated.
    pub(crate) fn fill_mapping(self, result: &Bound<'_, PyDict>) -> PyResult<()> {
        let Self::Mapping(members) = self else {
            return Err(PyValueError::new_err("parameter payload is not a mapping"));
        };
        for (key, value) in members {
            result.set_item(key.bind(result.py()), value.bind(result.py()))?;
        }
        Ok(())
    }
    pub(crate) fn materialize(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::Mapping(members) => {
                let result = PyDict::new(py);
                for (key, value) in members {
                    result.set_item(key.bind(py), value.bind(py))?;
                }
                Ok(result.into_any().unbind())
            }
            Self::Sequence(members, true) => Ok(PyTuple::new(py, members)?.into_any().unbind()),
            Self::Sequence(members, false) => Ok(PyList::new(py, members)?.into_any().unbind()),
        }
    }
}

#[pymethods]
impl DenseCallSpecFamily {
    #[new]
    fn new(py: Python<'_>, callspecs: &Bound<'_, PyAny>) -> PyResult<Self> {
        let constructor = py.import("_pytest.python")?.getattr("CallSpec2")?;
        let raw_rows = callspecs.try_iter()?.map(|callspec| {
            let callspec = callspec?;
            if !callspec.is_exact_instance(&constructor) {
                return Err(PyTypeError::new_err(
                    "dense family requires exact CallSpec2 objects",
                ));
            }
            callspec
                .getattr("__dict__")?
                .cast_into::<PyDict>()
                .map_err(Into::into)
        });
        Self::from_raw(py, raw_rows)
    }

    fn __len__(&self) -> usize {
        self.rows.len()
    }

    fn has_ids(&self, row: usize) -> PyResult<bool> {
        Ok(self.row(row)?.ids.len != 0)
    }

    fn retained(&self, row: usize, name: &str) -> PyResult<bool> {
        self.row(row)?;
        Ok(self.observed.contains_key(&(row, Field::parse(name)?)))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "the PyO3 receiver stays owned while field allocation can reenter Python"
    )]
    fn field(slf: Py<Self>, py: Python<'_>, row: usize, name: &str) -> PyResult<Py<PyAny>> {
        let field = Field::parse(name)?;
        let snapshot = {
            let state = slf.borrow(py);
            if state.anchored {
                return Err(PyValueError::new_err(
                    "anchored fields are owned by the callspec header",
                ));
            }
            if let Some(value) = state.observed.get(&(row, field)) {
                return Ok(value.clone_ref(py));
            }
            state.snapshot(py, row, field)?
        };
        let candidate = snapshot.materialize(py)?;
        let (value, released) = {
            let mut state = slf.borrow_mut(py);
            state.row(row)?;
            if let Some(value) = state.observed.get(&(row, field)) {
                (value.clone_ref(py), Vec::new())
            } else {
                state.observed.insert((row, field), candidate.clone_ref(py));
                (candidate.clone_ref(py), state.release(row, field)?)
            }
        };
        drop((candidate, released));
        Ok(value)
    }

    // Return the displaced container so any destructor runs after PyO3 releases
    // the mutable arena borrow, allowing it to re-enter a saved callspec.
    fn set_field(&mut self, row: usize, name: &str, value: Py<PyAny>) -> PyResult<Vec<Py<PyAny>>> {
        if self.anchored {
            return Err(PyValueError::new_err(
                "anchored mutable fields are owned by their header",
            ));
        }
        self.row(row)?;
        let field = Field::parse(name)?;
        let previous = self.observed.insert((row, field), value);
        match previous {
            Some(previous) => Ok(vec![previous]),
            None => self.release(row, field),
        }
    }

    pub(crate) fn detach(&mut self, row: usize) -> PyResult<Vec<Py<PyAny>>> {
        self.row(row)?;
        let mut released = Vec::new();
        for field in [
            Field::Params,
            Field::Indices,
            Field::Scopes,
            Field::Ids,
            Field::Marks,
        ] {
            if self.rows[row].promoted & field_bit(field) != 0 {
                continue;
            }
            if let Some(value) = self.observed.remove(&(row, field)) {
                released.push(value);
            } else {
                released.extend(self.release(row, field)?);
            }
        }
        self.rows[row].active = false;
        Ok(released)
    }

    fn id(slf: PyRef<'_, Self>, py: Python<'_>, row: usize) -> PyResult<Py<PyAny>> {
        let row_data = slf.row(row)?;
        if row_data.promoted & field_bit(Field::Ids) != 0 {
            return Err(PyValueError::new_err(
                "callspec IDs are owned by the header",
            ));
        }
        let cached = slf
            .observed
            .get(&(row, Field::Ids))
            .map(|value| value.clone_ref(py));
        let members = if cached.is_none() {
            slf.cells[row_data.ids.range()]
                .iter()
                .map(|index| slf.object(*index).map(|value| value.clone_ref(py)))
                .collect::<PyResult<Vec<_>>>()?
        } else {
            Vec::new()
        };
        drop(slf);
        let ids = match cached {
            Some(value) => value,
            None => PyTuple::new(py, members)?.into_any().unbind(),
        };
        let separator = PyString::new(py, "-");
        Ok(separator.call_method1("join", (ids.bind(py),))?.unbind())
    }

    #[pyo3(signature = (row, function_scope=None))]
    pub(crate) fn function_scoped(
        slf: PyRef<'_, Self>,
        py: Python<'_>,
        row: usize,
        function_scope: Option<Py<PyAny>>,
    ) -> PyResult<bool> {
        let row_data = slf.row(row)?;
        if row_data.promoted & (field_bit(Field::Indices) | field_bit(Field::Scopes)) != 0 {
            return Ok(false);
        }
        if !slf.observed.contains_key(&(row, Field::Indices))
            && !slf.observed.contains_key(&(row, Field::Scopes))
        {
            return slf.untouched_function_scoped(py, row_data, function_scope.as_ref());
        }
        let names = if let Some(indices) = slf.observed.get(&(row, Field::Indices)) {
            let Ok(indices) = indices.bind(py).cast_exact::<PyDict>() else {
                return Ok(false);
            };
            if indices
                .iter()
                .any(|(name, _)| !name.is_exact_instance_of::<PyString>())
            {
                return Ok(false);
            }
            indices
                .iter()
                .map(|(name, _)| name.unbind())
                .collect::<Vec<_>>()
        } else {
            slf.shapes[row_data.indices.shape as usize]
                .iter()
                .map(|name| slf.object(*name).map(|value| value.clone_ref(py)))
                .collect::<PyResult<Vec<_>>>()?
        };
        if names.is_empty() {
            return Ok(true);
        }
        let observed = slf
            .observed
            .get(&(row, Field::Scopes))
            .map(|value| value.clone_ref(py));
        let scopes = if observed.is_none() {
            slf.shapes[row_data.scopes.shape as usize]
                .iter()
                .zip(&slf.cells[row_data.scopes.values.range()])
                .map(|(name, scope)| {
                    Ok((
                        slf.object(*name)?.clone_ref(py),
                        slf.object(*scope)?.clone_ref(py),
                    ))
                })
                .collect::<PyResult<Vec<_>>>()?
        } else {
            Vec::new()
        };
        let function = match function_scope {
            Some(scope) => scope,
            None => slf
                .function_scope
                .as_ref()
                .ok_or_else(|| PyValueError::new_err("callspec family was cleared"))?
                .clone_ref(py),
        };
        drop(slf);
        let function = function.bind(py);
        if let Some(observed) = observed {
            let Ok(scopes) = observed.bind(py).cast_exact::<PyDict>() else {
                return Ok(false);
            };
            // A promoted mapping owns its live values, including observer edits.
            if scopes
                .iter()
                .any(|(name, _)| !name.is_exact_instance_of::<PyString>())
            {
                return Ok(false);
            }
            for name in names {
                let Some(scope) = scopes.get_item(name.bind(py))? else {
                    return Ok(false);
                };
                if !scope.is(function) {
                    return Ok(false);
                }
            }
        } else {
            for name in names {
                let mut found = false;
                for (scope_name, scope) in &scopes {
                    if name.bind(py).eq(scope_name.bind(py))? {
                        if !scope.bind(py).is(function) {
                            return Ok(false);
                        }
                        found = true;
                        break;
                    }
                }
                if !found {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3's GC protocol owns PyVisit"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for object in &self.objects {
            visit.call(&object.value)?;
        }
        for object in self.observed.values() {
            visit.call(object)?;
        }
        visit.call(&self.function_scope)?;
        Ok(())
    }

    fn __clear__(mut slf: PyRefMut<'_, Self>) {
        let observed = std::mem::take(&mut slf.observed);
        let objects = std::mem::take(&mut slf.objects);
        let function_scope = slf.function_scope.take();
        slf.rows.clear();
        slf.cells.clear();
        slf.shapes.clear();
        drop(slf);
        drop(observed);
        drop(objects);
        drop(function_scope);
    }
}
