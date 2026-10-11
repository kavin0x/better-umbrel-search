from better_search.rrf import reciprocal_rank_fusion


def test_rrf_prefers_items_ranked_in_both_lists() -> None:
    fused = reciprocal_rank_fusion([[1, 2, 3], [1, 3, 4]])
    assert fused[0][0] == 1
    assert fused[0][1] > fused[1][1]


def test_rrf_ignores_empty_rankings() -> None:
    fused = reciprocal_rank_fusion([[], [5]])
    assert fused == [(5, 1 / 61)]
