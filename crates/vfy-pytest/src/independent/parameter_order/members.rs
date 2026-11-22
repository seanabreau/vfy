//! An index-linked list of parameter members that supports moving a member to the front.

use std::num::NonZeroUsize;

// A Vec cannot contain usize::MAX nodes, so its index plus one fits.
#[derive(Clone, Copy)]
pub(super) struct MemberId(NonZeroUsize);

impl MemberId {
    fn from_index(index: usize) -> Self {
        Self(NonZeroUsize::MIN.saturating_add(index))
    }

    fn index(self) -> usize {
        self.0.get() - 1
    }
}

struct MemberNode {
    item: usize,
    previous: Option<MemberId>,
    next: Option<MemberId>,
}

#[derive(Default)]
pub(super) struct Members {
    nodes: Vec<MemberNode>,
    head: Option<MemberId>,
    tail: Option<MemberId>,
}

impl Members {
    pub(super) fn push(&mut self, item: usize) -> MemberId {
        let node = MemberId::from_index(self.nodes.len());
        self.nodes.push(MemberNode {
            item,
            previous: self.tail,
            next: None,
        });
        if let Some(tail) = self.tail {
            self.nodes[tail.index()].next = Some(node);
        } else {
            self.head = Some(node);
        }
        self.tail = Some(node);
        node
    }

    pub(super) fn prioritize(&mut self, node: MemberId) {
        let Some(previous) = self.nodes[node.index()].previous else {
            return;
        };
        let next = self.nodes[node.index()].next;
        self.nodes[previous.index()].next = next;
        if let Some(next) = next {
            self.nodes[next.index()].previous = Some(previous);
        } else {
            self.tail = Some(previous);
        }
        self.nodes[node.index()].previous = None;
        self.nodes[node.index()].next = self.head;
        if let Some(head) = self.head {
            self.nodes[head.index()].previous = Some(node);
        }
        self.head = Some(node);
    }

    pub(super) fn iter(&self) -> impl Iterator<Item = usize> + '_ {
        let mut current = self.head;
        std::iter::from_fn(move || {
            let node = current?;
            current = self.nodes[node.index()].next;
            Some(self.nodes[node.index()].item)
        })
    }
}

#[cfg(test)]
#[path = "members/tests.rs"]
mod tests;
