use std::sync::Arc as Shared;

use super::{LineSetTable, NumbitsRangeError, NumbitsTable, SparseLines, validate_numbits_len};

#[test]
fn numbits_length_is_bounded_by_the_signed_line_range() {
    let maximum_bytes = usize::try_from(i32::MAX).expect("i32 fits in usize") / 8 + 1;
    assert_eq!(validate_numbits_len(0), Ok(()));
    assert_eq!(validate_numbits_len(maximum_bytes), Ok(()));
    assert_eq!(
        validate_numbits_len(maximum_bytes + 1),
        Err(NumbitsRangeError)
    );
    assert_eq!(validate_numbits_len(usize::MAX), Err(NumbitsRangeError));
}

#[test]
fn numbits_conversion_is_canonical_and_ignores_negative_sentinels() {
    let mut lines = SparseLines::default();
    lines.extend([-65, -1, 0, 1, 8, 9, 63, 64, 65]);
    assert_eq!(
        lines.to_numbits(),
        vec![
            0b0000_0011,
            0b0000_0011,
            0,
            0,
            0,
            0,
            0,
            0b1000_0000,
            0b0000_0011,
        ]
    );

    let mut negative = SparseLines::default();
    negative.extend([-65, -1]);
    assert_eq!(lines.min_line(), Some(-65));
    assert!(lines.contains(-1));
    assert!(lines.contains(65));
    assert!(!lines.contains(66));
    assert_eq!(negative.min_line(), Some(-65));
    assert_eq!(SparseLines::default().min_line(), None);
    assert!(negative.to_numbits().is_empty());
    assert!(SparseLines::default().to_numbits().is_empty());
}

#[test]
fn numbits_round_trip_preserves_lines_and_trims_trailing_zero_bytes() {
    let numbits = [
        0b0001_0110,
        0,
        0b1000_0001,
        0,
        0,
        0,
        0,
        0b1000_0000,
        0b1000_0001,
        0,
    ];
    let lines = SparseLines::from_numbits(&numbits).expect("valid line bits");
    assert_eq!(lines.sorted(), [1, 2, 4, 16, 23, 63, 64, 71]);
    assert_eq!(
        lines.iter().collect::<std::collections::BTreeSet<_>>(),
        [1, 2, 4, 16, 23, 63, 64, 71].into_iter().collect()
    );
    assert_eq!(lines.to_numbits(), numbits[..9]);
}

#[test]
fn line_set_table_shares_equal_sets_but_checks_hash_collisions() {
    let mut table = LineSetTable::default();
    let mut repeated = SparseLines::from_numbits(&[0b0010_0010]).expect("valid line bits");
    let mut same_with_padding =
        SparseLines::from_numbits(&[0b0010_0010, 0]).expect("valid line bits");
    table.share(&mut repeated);
    table.share(&mut same_with_padding);
    assert!(repeated.shares_allocation(&same_with_padding));

    let mut distinct = SparseLines::from_numbits(&[0b0100_0010]).expect("valid line bits");
    let collision = LineSetTable::fingerprint(&distinct);
    table
        .sets
        .insert(collision, vec![Shared::clone(&repeated.chunks)]);
    table.share(&mut distinct);
    assert!(!distinct.shares_allocation(&repeated));
    assert_eq!(distinct.sorted(), [1, 6]);
}

#[test]
fn numbits_conversion_preserves_copy_on_write_aliases() {
    let original = SparseLines::from_numbits(&[0b0000_0010]).expect("valid original line bits");
    let mut changed = original.clone();
    changed.extend_set(
        &SparseLines::from_numbits(&[0, 0b0000_0010]).expect("valid additional line bits"),
    );
    assert_eq!(original.to_numbits(), [0b0000_0010]);
    assert_eq!(changed.to_numbits(), [0b0000_0010, 0b0000_0010]);
}

#[test]
fn numbits_table_shares_equal_sets_by_exact_position_and_bytes() {
    let mut table = NumbitsTable::default();
    let first = table.decode(&[0b0010_0010]).expect("valid line bits");
    let padded = table.decode(&[0b0010_0010, 0, 0]).expect("valid line bits");
    assert!(first.shares_allocation(&padded));

    let shifted = table
        .decode(&[0, 0, 0, 0, 0, 0, 0, 0, 0b0010_0010])
        .expect("valid line bits");
    assert!(!shifted.shares_allocation(&first));
    assert_eq!(shifted.sorted(), [65, 69]);
    let shifted_again = table
        .decode(&[0, 0, 0, 0, 0, 0, 0, 0, 0b0010_0010, 0])
        .expect("valid line bits");
    assert!(shifted_again.shares_allocation(&shifted));

    let collision = NumbitsTable::fingerprint(0, &[0b0100_0010]);
    table.first.insert(collision, 0);
    let distinct = table.decode(&[0b0100_0010]).expect("valid line bits");
    assert!(!distinct.shares_allocation(&first));
    assert_eq!(distinct.sorted(), [1, 6]);
    let empty = table.decode(&[0, 0]).expect("valid empty line bits");
    assert!(empty.sorted().is_empty());
}
