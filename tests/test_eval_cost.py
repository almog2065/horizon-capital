from evals.metrics import compute_cost
from evals.replay import replay


def test_eval_cost_from_replay():
    result = replay("sample")
    cost = compute_cost(result.traces)
    assert cost.n_llm_calls >= 1
    assert cost.total_usd > 0
    assert cost.by_purpose
