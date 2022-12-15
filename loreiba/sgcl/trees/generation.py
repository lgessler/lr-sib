import random
from typing import Any, Dict, List, Set

import torch

from loreiba.sgcl.trees.common import TreeSgclConfig


def immediate_children(head_map: Dict[int, int | None], token_id: int):
    return [child for child, parent in head_map.items() if parent == token_id]


def subtree_of_id(head_map: Dict[int, int | None], token_id: int) -> Dict[int, int | None]:
    """Get the subtree rooted at a given ID."""
    subtree = {token_id: None}
    queue = immediate_children(head_map, token_id)
    while len(queue) > 0:
        token_id = queue.pop(0)
        subtree[token_id] = head_map[token_id]
        children = immediate_children(head_map, token_id)
        queue.extend(children)
    return subtree


def adjacent_ids_of_subtree(head_map: Dict[int, int | None], subtree_ids: Set[int]) -> Set[int]:
    """
    Return set of IDs that are adjacent to all IDs in a subtree
    and are NOT a direct ancestor of the subtree's root
    """
    adjacent = set()

    # Get parents
    parent_ids = set()
    current = tuple(subtree_ids)[0]
    while current is not None:
        parent_ids.add(current)
        current = head_map[current]

    # On to the main work
    for token_id in subtree_ids:
        left = token_id - 1
        right = token_id + 1
        for x in [left, right]:
            if x > 0 and x not in subtree_ids and x not in parent_ids and x in head_map.keys():
                adjacent.add(x)
    return adjacent


def get_all_subtrees(head_map: Dict[int, int | None]) -> Dict[int, Dict[int, int | None]]:
    subtrees = {}
    for token_id in range(1, len(head_map.items())):
        subtrees[token_id] = subtree_of_id(head_map, token_id)
    return subtrees


def get_eligible_subtrees(
    config: TreeSgclConfig, head_map: Dict[int, int | None], all_subtrees: Dict[int, Dict[int, int | None]]
) -> List[Dict[str, Any]]:
    """
    Given a tensor of shape [token_len], return all subtrees and subtrees eligible for the tree-based contrastive loss.
    """
    eligible = []

    for token_id in range(1, len(head_map.items())):
        subtree = all_subtrees[token_id]
        # IDs that are not in the subtree but neighbor at least one token in the subtree
        adjacent_ids = adjacent_ids_of_subtree(head_map, set(subtree.keys()))
        # We need at least one token to replace and one token to stay the same
        if len(subtree) < config.min_subtree_size:
            continue
        # We need at least one token to provide as a replacement
        if len(adjacent_ids) < 1:
            continue
        eligible.append(
            {
                "root_id": token_id,
                "subtree": subtree,
                "adjacent_ids": adjacent_ids,
            }
        )
    return eligible


################################################################################
# tree generation
################################################################################
def generate_negative_trees(
    config: TreeSgclConfig,
    all_subtrees: Dict[int, Dict[int, int | None]],
    root_id: int,
    subtree: Dict[int, int | None],
    adjacent_ids: Set[int],
) -> Dict[str, Any]:
    """
    There are potentially many subtrees, but we only want up to `config.max_negative_per_subtree`.
    Define a negative tree in terms of:
      (1) a list of exactly one, two, or three nodes
      (2) a list of adjacent nodes, equal in size to (1), which are the replacements for the leaf nodes in (1)
    We could try to generate all possible negative combinations of the two and sample from that, but
    for efficiency we're just going to sample and check for duplicates, breaking out of generation if we
    get a collision `max_retry` times in a row.
    """
    # A valid node for replacement is any one that is not the root of the subtree
    all_replacement_targets = tuple({k for k, v in subtree.items() if v is not None})
    # A valid replacement is an adjacent id
    all_replacement_values = tuple(adjacent_ids.copy())
    # 3 possible limiting reagents for replacements: targets, values, and the limit (a hyperparameter)
    sample_size = min(len(all_replacement_targets), len(all_replacement_values), config.max_replacements)

    already_done = set()
    negatives = []
    retry_count = 0
    while len(negatives) < config.max_negative_per_subtree:
        targets = tuple(sorted(random.sample(all_replacement_targets, sample_size)))
        values = tuple(sorted(random.sample(all_replacement_values, sample_size)))

        # We've sampled something we already saw. Stop trying if we've exceeded the limit, else try again.
        if (targets, values) in already_done:
            retry_count += 1
            if retry_count > config.max_retry:
                break
            else:
                continue
        # This is new--record that we saw it
        already_done.add((targets, values))

        # First, copy the subtree
        negative_tree = subtree.copy()
        for target, value in zip(targets, values):
            # Retrieve the subtree to be removed
            target_subtree = all_subtrees[target]
            # Note the head of the subtree we're deleting
            subtree_head_id = [k for k, v in target_subtree.items() if v is None][0]
            # Remove the target: find all the token IDs in the subtree and remove them
            for k in target_subtree.keys():
                # We might have already removed the subtree in a previous iteration, so check first
                if k in negative_tree:
                    del negative_tree[k]
            # Retrieve the subtree to be spliced into the negative tree
            replacement_subtree = all_subtrees[value]
            for token_id, head_id in replacement_subtree.items():
                # if we found the root of the replacement subtree, make its head the original subtree's head
                if head_id is None:
                    head_id = subtree_head_id
                negative_tree[token_id] = head_id
        negatives.append(negative_tree)

    return {
        "root_id": root_id,
        "positive": subtree,
        "negatives": negatives,
    }


def generate_subtrees(config: TreeSgclConfig, head: torch.LongTensor) -> List[List[Dict[str, Any]]]:
    """
    Generate pairs of positive and negative trees
    """
    # Count number of tokens in the tree: find nonzero heads to account for 0-padding, and add one
    # to account for the fact that 0:root is labeled with a head = 0.
    token_counts = (head != 0).sum(1) + 1

    # split the batched head tensor into one tensor per input sequence, with padding removed
    padless_head = [head[i, : token_counts[i]] for i, x in enumerate(token_counts)]
    # Map from IDs to heads. Note that this is all 1-indexed, with 0 being the dummy ROOT node.
    head_map = [{0: None, **{i + 1: h.item() for i, h in enumerate(heads)}} for heads in padless_head]

    # get eligible subtrees for each sequence
    all_subtree_lists = [get_all_subtrees(s) for s in head_map]
    eligible_subtree_lists = [
        get_eligible_subtrees(config, s, all_subtrees) for s, all_subtrees in zip(head_map, all_subtree_lists)
    ]
    subtree_lists = [config.subtree_sampling_method.sample(subtree_list) for subtree_list in eligible_subtree_lists]
    tree_sets = []
    # For each sentence in the batch...
    for subtree_list, all_subtrees in zip(subtree_lists, all_subtree_lists):
        positive_and_negative_trees = []
        # For each subtree in the sentence...
        for subtree in subtree_list:
            # Collect negative trees with the positive tree
            positive_and_negative_trees.append(generate_negative_trees(config, all_subtrees, **subtree))
        tree_sets.append(positive_and_negative_trees)
    return tree_sets
