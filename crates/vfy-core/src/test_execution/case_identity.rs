//! Compiled test indexes recorded on test results.

use super::CaseResult;
use crate::test_plan::TestId;

impl CaseResult {
    /// Return the compiled test index recorded when the case ran.
    ///
    /// A decoded index grants no live invocation or worker permit.
    #[must_use]
    pub fn test_id(&self) -> Option<TestId> {
        self.test_id
    }

    /// Record the compiled test index for this case.
    pub fn set_test_id(&mut self, test: TestId) {
        self.test_id = Some(test);
    }
}

/// Find the result case recorded for a compiled test, without using the display node ID.
///
/// # Errors
/// Returns an error when no case or more than one case records the test.
pub fn case_for_test(cases: &[CaseResult], test: TestId) -> Result<usize, &'static str> {
    let mut found = None;
    for (index, case) in cases.iter().enumerate() {
        if case.test_id() == Some(test) && found.replace(index).is_some() {
            return Err("compiled test identity appears in multiple result cases");
        }
    }
    found.ok_or("compiled test identity has no emitted result case")
}

#[cfg(test)]
mod tests;
