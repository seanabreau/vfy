use super::{Membership, Resources, members::Members};
use std::collections::{BTreeSet, VecDeque};

// A plain vector implementation serves as the reference model for `Members`.
struct VectorModel {
    item_keys: Vec<Vec<Vec<usize>>>,
    members: Vec<Vec<Vec<usize>>>,
}

impl VectorModel {
    fn prioritize(&mut self, item: usize) {
        for rank in 1..5 {
            for key in &self.item_keys[rank][item] {
                let members = &mut self.members[rank][*key];
                if let Some(position) = members.iter().position(|member| *member == item) {
                    members.remove(position);
                }
                members.insert(0, item);
            }
        }
    }

    fn arrange(&mut self, input: Vec<usize>, rank: usize) -> Vec<usize> {
        if rank == 0 || input.len() < 3 {
            return input;
        }
        if self.members[rank].is_empty() {
            return self.arrange(input, rank - 1);
        }
        let allowed = input.iter().copied().collect::<BTreeSet<_>>();
        let mut pending = VecDeque::from(input);
        let mut ignored = BTreeSet::new();
        let mut emitted = BTreeSet::new();
        let mut output = Vec::new();
        while !pending.is_empty() {
            let mut prefix = Vec::new();
            let mut prefix_set = BTreeSet::new();
            let mut selected = None;
            while let Some(item) = pending.pop_front() {
                if emitted.contains(&item) || !prefix_set.insert(item) {
                    continue;
                }
                let key = self.item_keys[rank][item]
                    .iter()
                    .rev()
                    .find(|key| !ignored.contains(*key))
                    .copied();
                if let Some(key) = key {
                    let matching = self.members[rank][key]
                        .iter()
                        .filter(|item| allowed.contains(item))
                        .copied()
                        .collect::<Vec<_>>();
                    for member in matching.into_iter().rev() {
                        pending.push_front(member);
                        self.prioritize(member);
                    }
                    selected = Some(key);
                    break;
                }
                prefix.push(item);
            }
            for item in self.arrange(prefix, rank - 1) {
                if emitted.insert(item) {
                    output.push(item);
                }
            }
            if let Some(key) = selected {
                ignored.insert(key);
            }
        }
        output
    }
}

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
fn linked_resource_grouping_matches_vector_model_for_permutations() {
    permutations(&mut [0, 1, 2, 3, 4], 0, &mut |input| {
        let mut model = VectorModel {
            item_keys: vec![vec![Vec::new(); 5]; 5],
            members: vec![Vec::new(); 5],
        };
        let mut resources = Resources {
            item_keys: vec![vec![Vec::new(); 5]; 5],
            members: (0..5).map(|_| Vec::new()).collect(),
        };
        // Overlapping keys at several scopes exercise recursive grouping and moves.
        for (rank, groups) in [
            (1, vec![vec![0, 2, 4], vec![1, 3]]),
            (2, vec![vec![0, 1, 3], vec![2, 4]]),
            (4, vec![vec![0, 1, 2], vec![1, 3, 4]]),
        ] {
            for group in groups {
                let key = model.members[rank].len();
                let mut members = Members::default();
                for &item in &group {
                    model.item_keys[rank][item].push(key);
                    let node = members.push(item);
                    resources.item_keys[rank][item].push(Membership { key, node });
                }
                model.members[rank].push(group);
                resources.members[rank].push(members);
            }
        }
        assert_eq!(
            resources.arrange(input.to_vec(), 4),
            model.arrange(input.to_vec(), 4)
        );
        for rank in 1..5 {
            for (members, expected) in resources.members[rank].iter().zip(&model.members[rank]) {
                assert_eq!(members.iter().collect::<Vec<_>>(), *expected);
            }
        }
    });
}
