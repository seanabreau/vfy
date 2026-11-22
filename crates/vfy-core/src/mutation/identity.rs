//! Cache identities for complete and incremental mutation runs.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::json;

use super::MutationTimeoutPolicy;
use super::cache::MutationInput;

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum MutationPlanScope {
    Selected,
    FullSuite,
    Unreached,
}

#[derive(Deserialize)]
struct FingerprintInput {
    version: String,
    implementation: String,
    python_version: String,
    mutations: Vec<MutationInput>,
    tests: Vec<String>,
    pytest_args: Vec<String>,
    timeout: String,
    #[serde(default)]
    timeout_policy: MutationTimeoutPolicy,
    evidence: BTreeMap<String, Vec<String>>,
    scopes: BTreeMap<String, MutationPlanScope>,
    suite_state: Vec<(String, String)>,
}

impl FingerprintInput {
    fn fingerprint(
        &self,
        mutations: &[MutationInput],
        tests: &[String],
        evidence: &BTreeMap<String, Vec<String>>,
        scopes: &BTreeMap<String, MutationPlanScope>,
    ) -> Result<String, String> {
        let mut tests = tests.to_vec();
        tests.sort();
        let evidence: Vec<_> = evidence
            .iter()
            .map(|(key, values)| {
                let mut values = values.clone();
                values.sort();
                (key, values)
            })
            .collect();
        let encode = |value| crate::python_json(&value, false).map_err(|error| error.to_string());
        let compact = |value| crate::python_compact_json(&value).map_err(|error| error.to_string());
        Ok(crate::replay_fingerprint([
            self.version.clone(),
            "isolated-pytest-asyncio".to_owned(),
            self.implementation.clone(),
            self.python_version.clone(),
            encode(json!(mutations))?,
            encode(json!(tests))?,
            compact(json!(evidence))?,
            compact(json!(scopes))?,
            encode(json!(self.pytest_args))?,
            self.timeout.clone(),
            encode(json!(self.timeout_policy))?,
            compact(json!(self.suite_state))?,
        ]))
    }
}

/// Hash the complete run inputs and the inputs attributed to each mutation.
///
/// # Errors
/// Rejects malformed inputs or a mutation with no listed covering tests.
pub fn mutation_fingerprints(request: &str) -> Result<(String, BTreeMap<String, String>), String> {
    let input: FingerprintInput =
        serde_json::from_str(request).map_err(|error| error.to_string())?;
    let whole = input.fingerprint(
        &input.mutations,
        &input.tests,
        &input.evidence,
        &input.scopes,
    )?;
    let mut individual = BTreeMap::new();
    for mutation in &input.mutations {
        let tests = input
            .evidence
            .get(&mutation.id)
            .ok_or_else(|| format!("no covering tests listed for mutation {}", mutation.id))?;
        let evidence = BTreeMap::from([(mutation.id.clone(), tests.clone())]);
        let scope = input
            .scopes
            .get(&mutation.id)
            .ok_or_else(|| format!("missing mutation scope for {}", mutation.id))?;
        let scopes = BTreeMap::from([(mutation.id.clone(), scope.clone())]);
        individual.insert(
            mutation.id.clone(),
            input.fingerprint(std::slice::from_ref(mutation), tests, &evidence, &scopes)?,
        );
    }
    Ok((whole, individual))
}

/// Validate a selected mutation scope and keep it in collection order.
#[must_use]
pub fn mutation_collection_order(nodeids: &[String], selected: &[String]) -> Option<Vec<String>> {
    let collected = nodeids.iter().collect::<BTreeSet<_>>();
    let requested = selected.iter().collect::<BTreeSet<_>>();
    (collected.len() == nodeids.len()
        && requested.len() == selected.len()
        && nodeids.iter().all(|nodeid| !nodeid.is_empty())
        && requested.is_subset(&collected))
    .then(|| {
        nodeids
            .iter()
            .filter(|nodeid| requested.contains(nodeid))
            .cloned()
            .collect()
    })
}

/// Partition authenticated fresh executions by their complete ordered collection.
///
/// The caller owns the native collection witness. This helper deliberately has
/// no Python export: strings decoded from a result cannot establish authority.
///
/// # Errors
/// Rejects malformed fingerprint inputs or incomplete/repeated collection IDs.
pub fn fresh_full_suite_mutation_fingerprints(
    request: &str,
    nodeids: &[String],
    execution_scope: &str,
) -> Result<(String, BTreeMap<String, String>), String> {
    let mut input: FingerprintInput =
        serde_json::from_str(request).map_err(|error| error.to_string())?;
    let unique: BTreeSet<_> = nodeids.iter().collect();
    if nodeids.is_empty()
        || unique.len() != nodeids.len()
        || nodeids.iter().any(String::is_empty)
        || execution_scope.is_empty()
    {
        return Err("fresh full-suite identity requires complete unique collection IDs".to_owned());
    }
    input.tests = nodeids.to_vec();
    for mutation in &input.mutations {
        let evidence = input
            .evidence
            .get_mut(&mutation.id)
            .ok_or_else(|| format!("no covering tests listed for mutation {}", mutation.id))?;
        *evidence = if evidence.is_empty() || mutation.owner.phase != super::MutationPhase::Body {
            nodeids.to_vec()
        } else {
            mutation_collection_order(nodeids, evidence).unwrap_or_else(|| nodeids.to_vec())
        };
    }
    let ordered = crate::python_compact_json(&json!(nodeids)).map_err(|error| error.to_string())?;
    let partition = |fingerprint| {
        crate::replay_fingerprint([
            fingerprint,
            "independent-fresh-scope-v2".to_owned(),
            ordered.clone(),
            execution_scope.to_owned(),
        ])
    };
    let whole = partition(input.fingerprint(
        &input.mutations,
        &input.tests,
        &input.evidence,
        &input.scopes,
    )?);
    let mut individual = BTreeMap::new();
    for mutation in &input.mutations {
        let tests = input
            .evidence
            .get(&mutation.id)
            .ok_or_else(|| format!("no covering tests listed for mutation {}", mutation.id))?;
        let evidence = BTreeMap::from([(mutation.id.clone(), tests.clone())]);
        let scope = input
            .scopes
            .get(&mutation.id)
            .ok_or_else(|| format!("missing mutation scope for {}", mutation.id))?;
        let scopes = BTreeMap::from([(mutation.id.clone(), scope.clone())]);
        individual.insert(
            mutation.id.clone(),
            partition(input.fingerprint(
                std::slice::from_ref(mutation),
                tests,
                &evidence,
                &scopes,
            )?),
        );
    }
    Ok((whole, individual))
}
