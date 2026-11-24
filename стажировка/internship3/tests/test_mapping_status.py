"""Mapping of the model's JSON to database fields and the status decision (4.6)."""
import pytest

import impressum as imp
from status import PENDING_NAME, Status, status_name

FULL = {"company_name": "Muster GmbH",
        "postal_address": {"street": "Musterstraße", "house_no": "12", "postcode": "10115",
                           "city": "Berlin", "country": "Deutschland"},
        "handelsregister": "HRB 123456 B", "vat_number": "DE123456789", "phone": "+49 30 1234567",
        "email": "Info@Muster.de", "responsible_person": "Max Mustermann"}


def status_of(data):
    return imp.decide_status(imp.normalize_extraction(data))


def test_status_codes_are_the_ones_from_the_task():
    assert [(s.name, int(s)) for s in Status] == [
        ("OK", 0), ("NO_IMPRESSUM_LINK_FOUND", 1), ("PARSING_FAILED", 2),
        ("ADDRESS_INCOMPLETE", 3), ("ERROR", 4), ("NO_WEBSITE", 5)]
    assert status_name(None) == PENDING_NAME == "PENDING"
    assert status_name(1) == "NO_IMPRESSUM_LINK_FOUND" and status_name(99) == "UNKNOWN(99)"


# ----------------------------------------------------------------- status decision
def test_ok_when_fields_detected_and_address_complete():
    assert status_of(FULL) is Status.OK


@pytest.mark.parametrize("data", [{}, {"company_name": None}, {"postal_address": {}},
                                  {"company_name": "", "email": " ", "phone": "null",
                                   "postal_address": {"street": None, "city": ""}},
                                  [], "text", None])
def test_parsing_failed_when_nothing_was_detected(data):
    assert status_of(data) is Status.PARSING_FAILED


@pytest.mark.parametrize("missing", ["street", "house_no", "postcode", "city"])
def test_address_incomplete_when_any_of_the_four_parts_is_missing(missing):
    data = {**FULL, "postal_address": {**FULL["postal_address"], missing: None}}
    assert status_of(data) is Status.ADDRESS_INCOMPLETE


def test_country_is_not_required_for_a_complete_address():
    data = {**FULL, "postal_address": {**FULL["postal_address"], "country": None}}
    assert status_of(data) is Status.OK


def test_missing_vat_phone_person_do_not_downgrade_a_complete_address():
    data = {**FULL, "vat_number": None, "phone": None, "responsible_person": None,
            "handelsregister": None, "email": None}
    assert status_of(data) is Status.OK


def test_fields_without_any_address_is_incomplete_not_failed():
    assert status_of({"phone": "+49 30 1", "email": "a@b.de"}) is Status.ADDRESS_INCOMPLETE
    assert status_of({"company_name": "Muster GmbH"}) is Status.ADDRESS_INCOMPLETE


def test_partial_address_alone_counts_as_detected():
    assert status_of({"postal_address": {"city": "Berlin"}}) is Status.ADDRESS_INCOMPLETE


# ----------------------------------------------------------------- normalisation
def test_normalize_is_tolerant():
    n = imp.normalize_extraction({"postal_address": "Musterstr. 1, Berlin", "email": "  A@B.DE ",
                                  "phone": 12345, "vat_number": "N/A"})
    assert n["email"] == "a@b.de" and n["phone"] == "12345" and n["vat_number"] is None
    assert all(v is None for v in n["postal_address"].values())     # a string is not an address dict
    assert set(n) == {"company_name", "postal_address", "handelsregister", "vat_number",
                      "phone", "email", "responsible_person"}


def test_numeric_postcode_becomes_a_string():
    n = imp.normalize_extraction({"postal_address": {"postcode": 10115, "house_no": 12}})
    assert n["postal_address"]["postcode"] == "10115" and n["postal_address"]["house_no"] == "12"


def test_has_detected_fields():
    assert not imp.has_detected_fields(imp.normalize_extraction({}))
    assert imp.has_detected_fields(imp.normalize_extraction({"phone": "+49 1"}))


# ----------------------------------------------------------------- database mapping
def test_postal_address_is_joined_into_one_string():
    assert imp.format_postal_address(FULL["postal_address"]) == "Musterstraße 12, 10115 Berlin, Deutschland"
    assert imp.format_postal_address({"street": "Weg", "house_no": None, "postcode": None,
                                      "city": "Bonn", "country": None}) == "Weg, Bonn"
    assert imp.format_postal_address({}) is None


def test_to_db_fields_mapping():
    fields = imp.to_db_fields(imp.normalize_extraction(FULL), "CSV Name")
    assert fields == {
        "legal_name": "Muster GmbH", "postal_address": "Musterstraße 12, 10115 Berlin, Deutschland",
        "handelsregister": "HRB 123456 B", "vat_number": "DE123456789", "phone": "+49 30 1234567",
        "email": "info@muster.de", "responsible_person": "Max Mustermann"}


def test_legal_name_falls_back_to_the_csv_name():
    assert imp.to_db_fields(imp.normalize_extraction({"phone": "+49 1"}), "CSV Name")["legal_name"] == "CSV Name"
