from devmem.config import RRF_K
from devmem.search import rrf_merge


def test_rrf_merges_two_rankings():
    # k = 60. Scores: id 2 is ahead of 1, then 4, then 3.
    assert rrf_merge([[1, 2, 3], [2, 4, 1]], k=60) == [2, 1, 4, 3]


def test_rrf_default_constant_matches_the_plan():
    assert RRF_K == 60
    assert rrf_merge([[5, 1], [1, 5]]) == rrf_merge([[5, 1], [1, 5]], k=RRF_K)


def test_rrf_counts_a_duplicate_once_at_its_best_rank():
    assert rrf_merge([[1, 1, 2]], k=60) == [1, 2]


def test_rrf_ignores_empty_rankings():
    assert rrf_merge([[], [3, 1]], k=60) == [3, 1]
    assert rrf_merge([], k=60) == []
