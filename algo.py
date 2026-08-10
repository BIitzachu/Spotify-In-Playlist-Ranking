from __future__ import annotations

import random
from typing import Any


def _normalize_items(items):
    """Return a list view of items while preserving order where possible."""
    if items is None:
        return []
    if isinstance(items, dict):
        return list(items.keys())
    return list(items)


def _build_known_relations(sorted_subsections):
    """
    Build pairwise known ordering relations from prior sorted subsections.

    Subsections are processed in the order given, and a later subsection's
    direct statement about a pair overwrites an earlier one. This lets
    re-sorting the same two songs later (e.g. editing a saved subsection,
    or a taste change) take precedence over stale data, rather than the
    two data points silently conflicting.
    """
    relations = {}
    for subsection in sorted_subsections or []:
        arr = list(subsection)
        n = len(arr)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = arr[i], arr[j]
                relations[(a, b)] = -1  # a ranked before b
                relations[(b, a)] = 1   # b ranked after a
    return relations


def _build_relation_ranks(sorted_subsections):
    """
    For every direct "a before b" edge, record the index (in `sorted_subsections`
    order) of the *last* subsection that asserted it — i.e. how recent it is.
    Used to break cycles in favor of newer data, the same way a later
    subsection already overwrites an earlier direct statement about the
    same pair in `_build_known_relations`.
    """
    ranks: dict[tuple, int] = {}
    for rank, subsection in enumerate(sorted_subsections or []):
        arr = list(subsection)
        n = len(arr)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = arr[i], arr[j]
                ranks[(a, b)] = rank
    return ranks


def _closure_relations(items, relations, ranks=None):
    """
    Compute the transitive closure of `relations`, restricted to `items`.

    Two songs can be "known" to be ordered even if they were never placed
    in the same subsection together, as long as a chain of direct
    relations connects them (A before B, B before C => A before C). This
    also holds across subsections gathered from *different* playlists,
    since relations are keyed purely by song identity.

    Direct relations can disagree once enough of them pile up across
    different subsections (e.g. A<B, B<C, C<A pieced together from three
    separate subsections created at different times) — a genuine cycle,
    not just a restated pair. Rather than raising, the oldest edge on each
    such cycle is dropped (using `ranks`, the same recency signal that
    already lets a later subsection overwrite an earlier direct statement
    about the same pair) so newer judgments are trusted over older ones.
    If dropping still leaves a pair incomparable, it's simply left out of
    the closure and treated as unknown, so the caller will naturally ask
    the user to compare it again.

    Returns a dict {(a, b): -1 | 1, ...} covering every pair in `items`
    that is (consistently) comparable.
    """
    ranks = ranks or {}
    item_list = list(items)
    item_set = set(item_list)

    # Direct "before" edges, restricted to the relevant items.
    before: dict[Any, set] = {item: set() for item in item_list}
    for item in item_list:
        for other in item_list:
            if item == other:
                continue
            if relations.get((item, other)) == -1:
                before[item].add(other)

    # Break cycles: find a cycle via DFS, then drop the *oldest* edge on
    # that specific cycle (by rank) rather than an arbitrary one, so a
    # cycle formed from mostly-recent data only sacrifices its stalest
    # link. Repeat until the graph is acyclic.
    def _find_cycle_edge() -> tuple[Any, Any] | None:
        color = {item: 0 for item in item_list}  # 0=white,1=gray,2=black
        parent: dict[Any, Any] = {}

        for start in item_list:
            if color[start] != 0:
                continue
            stack = [(start, iter(before[start]))]
            color[start] = 1
            while stack:
                node, it = stack[-1]
                advanced = False
                for nxt in it:
                    if color[nxt] == 1:
                        # Back edge node->nxt closes a cycle. Walk parent
                        # links from node back up to nxt to collect every
                        # edge on the cycle, then pick the oldest one.
                        cycle_edges = [(node, nxt)]
                        cur = node
                        while cur != nxt:
                            p = parent[cur]
                            cycle_edges.append((p, cur))
                            cur = p
                        return min(cycle_edges, key=lambda e: ranks.get(e, -1))
                    if color[nxt] == 0:
                        color[nxt] = 1
                        parent[nxt] = node
                        stack.append((nxt, iter(before[nxt])))
                        advanced = True
                        break
                if not advanced:
                    color[node] = 2
                    stack.pop()
        return None

    while True:
        cycle_edge = _find_cycle_edge()
        if cycle_edge is None:
            break
        a, b = cycle_edge
        before[a].discard(b)

    # Transitive closure via reachability (DAG now, so this terminates).
    closure: dict[Any, set] = {}
    for item in item_list:
        seen: set = set()
        stack = list(before[item])
        while stack:
            node = stack.pop()
            if node in seen or node not in item_set:
                continue
            seen.add(node)
            stack.extend(before.get(node, ()))
        closure[item] = seen

    relations_out: dict[tuple, int] = {}
    for a, afters in closure.items():
        for b in afters:
            relations_out[(a, b)] = -1
            relations_out[(b, a)] = 1
    return relations_out


def _pick_informative_group(items, relations, size, preselected=None):
    """
    Greedily build a group of `size` items that maximizes still-unknown
    pairs among them — a "densest subgraph in the unknown-relations graph"
    heuristic. Unlike scanning only *contiguous* slices of `items`, this
    can combine items from anywhere in the collection, which matters: a
    sliding contiguous window can run out of new information to offer
    (every window it can form may already be fully known) while distant
    pairs are still genuinely unresolved, with no way to reach them. This
    construction only fails to make progress when no unknown pair exists
    anywhere — i.e. when the collection is already fully determined.

    `preselected`, if given, seeds the group instead of picking our own
    anchor — used to top up a group of randomly-chosen never-ranked items
    (see `_never_ranked_items`) with the most informative *known* items to
    pair them against, rather than discarding that random pick and
    starting over.
    """
    n = len(items)

    if preselected:
        chosen = list(preselected)
        chosen_set = set(chosen)
    else:
        # Anchor on the item with the most unknown relations overall — the
        # single best starting point for a productive group.
        def unknown_degree(a):
            return sum(1 for b in items if b != a and (a, b) not in relations)

        start = max(items, key=unknown_degree)
        chosen = [start]
        chosen_set = {start}

    while len(chosen) < size and len(chosen) < n:
        best_item = None
        best_new_unknown = -1
        for cand in items:
            if cand in chosen_set:
                continue
            new_unknown = sum(1 for c in chosen if (cand, c) not in relations)
            if new_unknown > best_new_unknown:
                best_new_unknown = new_unknown
                best_item = cand
        chosen.append(best_item)
        chosen_set.add(best_item)

    return chosen


def _never_ranked_items(items, sorted_subsections):
    """
    Items that have never appeared in *any* previously sorted subsection at
    all — not just ones with no known relation to other songs currently in
    `items`. A song can have "unknown" relations to everything in the
    current playlist while still being one the user has ranked plenty
    before (just never against these particular songs); this is the
    narrower set of songs that have literally never been put in front of
    the user for ranking, in any playlist, ever.
    """
    ever_ranked: set = set()
    for subsection in sorted_subsections or []:
        ever_ranked.update(subsection)
    return [item for item in items if item not in ever_ranked]


def _prepare(sorted_subsections, unsorted_dictionary) -> tuple[list, dict]:
    """Shared setup: normalize items and compute transitively-closed relations."""
    items = _normalize_items(unsorted_dictionary)
    direct = _build_known_relations(sorted_subsections)
    ranks = _build_relation_ranks(sorted_subsections)
    relations = _closure_relations(items, direct, ranks)
    return items, relations


def nextSubsectionToSort(sorted_subsections, unsorted_dictionary, subsection_size):
    """
    Choose the next subsection to sort from an unsorted collection.

    Args:
        sorted_subsections: list of already sorted subsections (iterables).
            These can come from *any* prior sort, not just ones involving
            items in `unsorted_dictionary` — subsections that reference
            items outside this collection are simply ignored, and any
            overlap still contributes useful known relations (directly or
            transitively).
        unsorted_dictionary: full unsorted collection (list/tuple/dict keys).
        subsection_size: size of subsection to return.

    Returns:
        A list containing the next subsection to sort, or an empty list if
        `unsorted_dictionary` is already fully sortable from known relations
        (check with `is_fully_determined` / `finalize_order` to get the
        actual resulting order in that case).

    Prioritizes surfacing songs that have never been ranked at all: while
    any exist, a random sample of them is favored over the purely
    informative "in-between" grouping (which tends to keep recombining
    songs already touched, since those are what carry the known relations
    it reasons about). Once every song has been seen at least once, this
    reduces to the "in-between" grouping alone. If a subsection would
    otherwise come up short of never-ranked songs, the remaining slots are
    filled in from the known items using the same informative-group logic,
    so the random pick still gets paired against something useful rather
    than sitting in an undersized group.
    """
    items, relations = _prepare(sorted_subsections, unsorted_dictionary)
    n = len(items)

    if n == 0:
        return []
    if subsection_size <= 0:
        return []
    if subsection_size >= n:
        return items[:]

    # If every pair is already comparable (directly or transitively), there's
    # nothing left to ask the user — the order can be finalized outright.
    if _all_pairs_known(items, relations):
        return []

    unseen = _never_ranked_items(items, sorted_subsections)
    if unseen:
        chosen = random.sample(unseen, min(subsection_size, len(unseen)))
        if len(chosen) < subsection_size:
            chosen = _pick_informative_group(items, relations, subsection_size, preselected=chosen)
        return chosen

    return _pick_informative_group(items, relations, subsection_size)


def _all_pairs_known(items, relations) -> bool:
    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            if (items[i], items[j]) not in relations:
                return False
    return True


def is_fully_determined(sorted_subsections, unsorted_dictionary) -> bool:
    """
    True if every pair in `unsorted_dictionary` is already comparable
    (directly or transitively) from `sorted_subsections`, meaning the full
    order can be produced without asking the user to sort anything else.

    Because relations are keyed by item identity rather than by playlist,
    this can be true the very first time a playlist is opened, if its
    songs were already fully related through subsections built while
    sorting other playlists.
    """
    items, relations = _prepare(sorted_subsections, unsorted_dictionary)
    if len(items) <= 1:
        return True
    return _all_pairs_known(items, relations)


def finalize_order(sorted_subsections, unsorted_dictionary):
    """
    Return the fully resolved order of `unsorted_dictionary`, or None if it
    isn't fully determined yet (i.e. `nextSubsectionToSort` still has more
    to ask). When not None, every item's relative position is backed by a
    known (direct or transitive) relation — nothing here is guessed.
    """
    items, relations = _prepare(sorted_subsections, unsorted_dictionary)
    n = len(items)

    if n <= 1:
        return items[:]
    if not _all_pairs_known(items, relations):
        return None

    def before_count(item):
        # In a fully-determined total order, the item ranked first is
        # "before" every other item; the item ranked last is before none.
        return sum(
            1 for other in items if other != item and relations.get((item, other)) == -1
        )

    return sorted(items, key=lambda item: -before_count(item))