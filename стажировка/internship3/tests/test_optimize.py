"""Token optimisation: threshold, head anchor, tail anchors, sandwich, fallbacks."""
import pytest

import impressum as imp
from tests.helpers import LEGAL_BLOCK, bloated_page

FILLER = "Dies ist ein neutraler Fülltext ohne rechtliche Angaben. "


def filler_lines(n, prefix="Zeile"):
    return "\n".join(f"{prefix} {i}: {FILLER}" for i in range(n))


# ----------------------------------------------------------------- threshold
def test_text_under_the_threshold_is_sent_unchanged():
    text = "Impressum\nMuster GmbH\nTel: +49 30 1234567"
    out, info = imp.optimize_text(text)
    assert out == text
    assert info["strategy"] == "unchanged"
    assert info["before_chars"] == info["after_chars"] == len(text)
    assert info["saved_pct"] == 0


def test_text_exactly_at_the_threshold_is_unchanged():
    text = "x" * imp.MAX_TEXT_CHARS
    assert imp.optimize_text(text)[0] == text


def test_threshold_is_configurable():
    text = bloated_page()
    short = imp.extract_visible_text(text)
    assert imp.optimize_text(short)[1]["strategy"] == "unchanged" or len(short) > imp.MAX_TEXT_CHARS
    out, info = imp.optimize_text(short, max_chars=500)
    assert info["strategy"] != "unchanged" and len(out) < len(short)


# ----------------------------------------------------------------- the sandwich
@pytest.fixture(scope="module")
def optimized_page():
    text = imp.extract_visible_text(bloated_page())
    assert len(text) > imp.MAX_TEXT_CHARS, "the test page must exceed the budget"
    return text, *imp.optimize_text(text)


def test_big_page_is_cut_to_the_legal_block(optimized_page):
    text, out, info = optimized_page
    assert info["strategy"] == "boilerplate+sandwich"
    assert info["after_chars"] < info["before_chars"] / 5          # > 80 % saved
    for must_keep in ("Impressum", "Muster Nutrition GmbH", "Musterstraße 12", "10115 Berlin",
                      "Max Mustermann", "+49 30 1234567", "Info@Muster-Nutrition.de",
                      "HRB 123456 B", "DE123456789"):
        assert must_keep in out
    assert "Wir sind ein junges Unternehmen" not in out             # head cut
    assert "Haftung für Inhalte" not in out                         # tail cut (disclaimers)
    assert "Footer Link" not in out and "Produkt Kategorie" not in out


def test_statistics_are_consistent(optimized_page):
    text, out, info = optimized_page
    assert info["before_chars"] == len(text) and info["after_chars"] == len(out)
    assert info["before_tokens"] == len(text) // 4 and info["after_tokens"] == len(out) // 4
    assert info["saved_pct"] == round(100 * (1 - len(out) / len(text)))
    assert info["head_found"] and info["tail_found"]
    line = imp.describe_optimization(info)
    assert f"{info['before_chars']} -> {info['after_chars']} chars" in line
    assert "tokens" in line and "%" in line


def test_estimate_tokens():
    assert imp.estimate_tokens("a" * 4000) == 1000 and imp.estimate_tokens("") == 0


# ----------------------------------------------------------------- tail anchors
@pytest.mark.parametrize("signal", [
    "Telefon: +49 30 1234567",
    "Telefon: 0049 30 1234567",
    "USt-IdNr.: DE123456789",
    "USt-IdNr.: DE 123456789",
    "E-Mail: kontakt@beispiel.de",
    "Handelsregister: HRB 99887 B",
    "Handelsregister: HRA 4711",
    "Registergericht: Amtsgericht München",
])
def test_each_strong_signal_ends_the_text(signal):
    text = f"Impressum\nMuster GmbH\nMusterstraße 1\n12345 Berlin\n{signal}\n{filler_lines(200)}"
    assert len(text) > imp.MAX_TEXT_CHARS
    out, info = imp.optimize_text(text)
    assert signal in out
    assert "Zeile 150" not in out and len(out) < 1200
    assert out.endswith(out.rstrip()) and info["strategy"].endswith("sandwich")


@pytest.mark.parametrize("label", ["Vertreten durch: Max Mustermann", "Geschäftsführer: Max Mustermann",
                                   "Verantwortlich für den Inhalt: Max Mustermann",
                                   "Inhaltlich verantwortlich: Max Mustermann"])
def test_responsibility_label_is_a_tail_anchor_too(label):
    # phone first, responsibility last -> the cut must come after the LAST anchor
    text = f"Impressum\nMuster GmbH\n+49 30 1234567\n{label}\n{filler_lines(200)}"
    out, _ = imp.optimize_text(text)
    assert label in out and "Zeile 150" not in out


def test_cut_is_a_margin_after_the_last_anchor():
    body = f"Impressum\nMuster GmbH\nTel: +49 30 1234567\n{filler_lines(200)}"
    anchor_end = body.index("+49 30 1234567") + len("+49 30 1234567")
    out, _ = imp.optimize_text(body, tail_margin=300)
    # everything up to anchor + margin (+ finishing the current line), nothing more
    assert len(out) <= anchor_end + 300 + 160
    assert len(out) > anchor_end - 100


def test_last_anchor_wins_over_earlier_ones():
    text = (f"Impressum\nMuster GmbH\nTel: +49 30 1234567\n{filler_lines(30)}\n"
            f"USt-IdNr.: DE123456789\n{filler_lines(200, 'Ende')}")
    out, _ = imp.optimize_text(text)
    assert "DE123456789" in out and "Zeile 15" in out      # the middle between the anchors stays


def test_footer_marker_after_the_last_anchor_cuts_early():
    text = (f"Impressum\nMuster GmbH\nTel: +49 30 1234567\nDatenschutz\nAGB\n© 2024\n"
            f"{filler_lines(200)}")
    out, _ = imp.optimize_text(text)
    assert out.rstrip().endswith("+49 30 1234567")


def test_disclaimer_heading_after_the_data_cuts_early():
    text = (f"Impressum\nMuster GmbH\nUSt-IdNr.: DE123456789\nHaftung für Inhalte\n"
            f"{filler_lines(200)}")
    out, _ = imp.optimize_text(text)
    assert out.rstrip().endswith("DE123456789")


def test_the_word_verantwortlich_inside_prose_does_not_drag_the_cut_to_the_end():
    prose = " ".join(["Wir sind für diese Inhalte nach den Gesetzen verantwortlich."] * 3)
    text = (f"Impressum\nMuster GmbH\nTel: +49 30 1234567\nZwischenüberschrift\n"
            + "\n".join(f"Absatz {i}: {prose}" for i in range(120)))
    out, _ = imp.optimize_text(text)
    assert "Absatz 100" not in out and len(out) < 1200


# ----------------------------------------------------------------- head anchor
@pytest.mark.parametrize("heading", ["Impressum", "Angaben gemäß § 5 TMG", "Angaben gemäß § 5 DDG",
                                     "Anbieter", "Diensteanbieter", "Imprint"])
def test_each_heading_opens_the_legal_section(heading):
    text = f"{filler_lines(150, 'Kopf')}\n{heading}\nMuster GmbH\nTel: +49 30 1234567\n{filler_lines(100)}"
    out, _ = imp.optimize_text(text)
    assert out.startswith(heading[:5]) or heading in out.split("\n")[0] or heading in out
    assert "Kopf 100" not in out and "Tel: +49 30 1234567" in out


def test_menu_item_far_before_the_data_is_not_the_head_anchor():
    text = (f"Impressum\n{filler_lines(200, 'Menü')}\nAngaben gemäß § 5 TMG\nMuster GmbH\n"
            f"Tel: +49 30 1234567\n{filler_lines(100)}")
    out, _ = imp.optimize_text(text)
    assert "Menü 100" not in out and out.startswith("Angaben gemäß § 5 TMG") or "Angaben" in out
    assert "Menü 100" not in out


def test_a_huge_single_line_before_the_heading_is_not_pulled_in():
    giant = "Wir sind ein junges Unternehmen. " * 400                      # ~13k chars, ONE line
    text = f"{giant}\nImpressum\nMuster GmbH\nTel: +49 30 1234567\n{filler_lines(100)}"
    out, _ = imp.optimize_text(text)
    assert "junges Unternehmen" not in out and "Tel: +49 30 1234567" in out and len(out) < 1000


# ----------------------------------------------------------------- relax / fallbacks
def test_head_only_when_there_is_no_signal():
    text = f"{filler_lines(150, 'Kopf')}\nImpressum\nMuster GmbH\n{filler_lines(60)}"
    out, info = imp.optimize_text(text)
    assert info["strategy"].endswith("head-cut") and "Kopf 100" not in out


def test_tail_only_when_there_is_no_head_anchor():
    text = f"{filler_lines(150, 'Kopf')}\nMuster GmbH\nTel: +49 30 1234567\n{filler_lines(200)}"
    out, info = imp.optimize_text(text)
    assert "tail-cut" in info["strategy"] and "Zeile 150" not in out   # tail cut, head kept
    assert "Tel: +49 30 1234567" in out and len(out) <= imp.MAX_TEXT_CHARS


def test_no_anchors_at_all_falls_back_to_a_head_tail_window():
    text = filler_lines(400)
    out, info = imp.optimize_text(text)
    assert "window" in info["strategy"] and len(out) <= imp.MAX_TEXT_CHARS + 10
    assert out.startswith("Zeile 0") and out.rstrip().endswith(text.rstrip()[-30:])
    assert "\n[...]\n" in out


def test_responsibility_only_page_is_relaxed_not_cut_blindly():
    # verification needs a phone/VAT/e-mail/registry signal; a lone label is not enough
    text = f"Impressum\nMuster GmbH\nGeschäftsführer: Max\n{filler_lines(200)}"
    out, info = imp.optimize_text(text)
    assert "relaxed-full" in info["strategy"]


def test_long_middle_is_reduced_with_key_lines_then_window():
    lines = [f"Zeile {i}: Kontakt unter mail{i}@beispiel.de oder Tel {i}" for i in range(400)]
    text = "Impressum\nMuster GmbH\n" + "\n".join(lines)
    out, info = imp.optimize_text(text)
    assert len(out) <= imp.MAX_TEXT_CHARS + 10
    assert "window" in info["strategy"]


def test_key_lines_keep_context_and_drop_the_rest():
    block = ("Impressum\nMuster GmbH\n" + filler_lines(80) +
             "\nMusterstraße 12\n10115 Berlin\nTel: +49 30 1234567\n" + filler_lines(80, "Ende"))
    out, info = imp.optimize_text(block, max_chars=900)
    assert "Musterstraße 12" in out and "+49 30 1234567" in out and len(out) <= 910


def test_optimising_never_makes_the_text_longer():
    for text in (imp.extract_visible_text(bloated_page()), filler_lines(400),
                 f"Impressum\n{LEGAL_BLOCK}\n{filler_lines(300)}"):
        out, info = imp.optimize_text(text)
        assert len(out) <= len(text) and info["after_chars"] == len(out)


def test_footer_marks_never_leak_into_the_text_sent_to_the_llm():
    text = (f"Impressum\nMuster GmbH\nTel: +49 30 1234567\nDatenschutz\nAGB\n"
            f"{filler_lines(200)}")
    out, _ = imp.optimize_text(text)
    assert imp.FOOTER_MARK not in out
    text2 = f"Impressum\nDatenschutz\n{filler_lines(300)}"
    assert imp.FOOTER_MARK not in imp.optimize_text(text2)[0]
