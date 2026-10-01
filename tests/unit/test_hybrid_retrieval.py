from app.retrieval.hybrid import reciprocal_rank_fusion


def test_rrf_combines_routes_and_breaks_ties_stably() -> None:
    fused = reciprocal_rank_fusion(
        [
            [("dense-first", 0.9), ("shared", 0.8)],
            [("sparse-first", 12.0), ("shared", 10.0)],
        ]
    )

    assert [chunk_id for chunk_id, _score in fused] == [
        "shared",
        "dense-first",
        "sparse-first",
    ]
