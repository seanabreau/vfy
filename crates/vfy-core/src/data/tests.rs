use super::encode_lines;

#[test]
fn packs_lines_and_ignores_negative_sentinels() {
    assert_eq!(
        encode_lines([-1, 0, 1, 8, 9]),
        vec![0b0000_0011, 0b0000_0011]
    );
    assert!(encode_lines([-2, -1]).is_empty());
}
