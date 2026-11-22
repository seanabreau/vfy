use super::Members;

fn permutations(values: &mut [usize], start: usize, visit: &mut impl FnMut(&[usize])) {
    if start == values.len() {
        visit(values);
        return;
    }
    for index in start..values.len() {
        values.swap(start, index);
        permutations(values, start + 1, visit);
        values.swap(start, index);
    }
}

#[test]
fn linked_members_match_vector_moves_for_all_small_permutations() {
    permutations(&mut [0, 1, 2, 3, 4], 0, &mut |moves| {
        let mut members = Members::default();
        let nodes = (0..5).map(|item| members.push(item)).collect::<Vec<_>>();
        let mut model = (0..5).collect::<Vec<_>>();
        for &item in moves.iter().chain(moves.iter().rev()) {
            let position = model
                .iter()
                .position(|value| *value == item)
                .expect("permutation item is present");
            model.remove(position);
            model.insert(0, item);
            members.prioritize(nodes[item]);
            assert_eq!(members.iter().collect::<Vec<_>>(), model);
        }
    });
}
