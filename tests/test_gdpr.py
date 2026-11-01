import pytest

from senda import gdpr, memory


def c(state="unknown"):
    return {"id": 1, "consent_state": state, "status": "active"}


@pytest.mark.parametrize("text", ["BAJA", "baja.", "Stop", "por favor dadme de baja", "no me escribáis más", "Borrad mis datos"])
def test_opt_out_detected_in_any_state(text):
    for state in ("unknown", "requested", "granted"):
        assert gdpr.evaluate_inbound(c(state), text) == "opt_out"


@pytest.mark.parametrize("text", ["SÍ", "si", "Sí, claro", "vale", "De acuerdo", "ok!"])
def test_yes_only_counts_when_requested(text):
    assert gdpr.evaluate_inbound(c("requested"), text) == "granted"
    assert gdpr.evaluate_inbound(c("unknown"), text) is None
    assert gdpr.evaluate_inbound(c("granted"), text) is None


def test_no_and_regrant():
    assert gdpr.evaluate_inbound(c("requested"), "No, gracias") == "denied"
    # "no" dentro de una frase larga no es una negativa de consentimiento
    assert gdpr.evaluate_inbound(c("requested"), "no sé si podré ir el martes por la mañana porque trabajo hasta tarde") is None
    assert gdpr.evaluate_inbound(c("revoked"), "ALTA") == "regrant"


def test_baja_in_unrelated_sentence_is_not_opt_out():
    assert gdpr.evaluate_inbound(c("granted"), "estoy de baja laboral, ¿podéis el jueves?") is None


def test_sensitive_facts_are_not_stored():
    assert memory.is_sensitive("estoy embarazada de 3 meses")
    assert not memory.is_sensitive("cobra el día 15")
