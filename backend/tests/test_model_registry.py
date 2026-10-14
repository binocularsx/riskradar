"""Model registry compatibility and explanation safeguards."""

from riskradar.features.spec import MODEL_FEATURE_NAMES
from riskradar.model.registry import ConstantScorer, ModelBundle


def stub_bundle() -> ModelBundle:
    return ModelBundle(
        id=1,
        name="stub",
        version="test",
        feature_spec_version="test",
        scorer=ConstantScorer(),
        calibration="none",
    )


def test_legacy_short_baseline_cannot_break_attributions():
    """A stale explanation baseline must not discard an otherwise valid score."""
    result = stub_bundle().attributions(
        [1.0] * len(MODEL_FEATURE_NAMES),
        [0.0] * 12,
    )

    assert list(result) == list(MODEL_FEATURE_NAMES)
    assert set(result.values()) == {0.0}


def test_wrong_model_vector_length_fails_with_a_clear_error():
    try:
        stub_bundle().attributions([1.0], [0.0])
    except ValueError as exc:
        assert "model vector has 1 values" in str(exc)
    else:
        raise AssertionError("a malformed model vector was accepted")
