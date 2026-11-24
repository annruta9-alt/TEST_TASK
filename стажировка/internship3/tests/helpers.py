"""HTML builders shared by the tests."""

LEGAL_BLOCK = """
<h1>Impressum</h1>
<h2>Angaben gemäß § 5 TMG</h2>
<p>Muster Nutrition GmbH<br>Musterstraße 12<br>10115 Berlin<br>Deutschland</p>
<h2>Vertreten durch</h2><p>Geschäftsführer: Max Mustermann</p>
<h2>Kontakt</h2><p>Telefon: +49 30 1234567<br>E-Mail: Info@Muster-Nutrition.de</p>
<h2>Registereintrag</h2><p>Registergericht: Amtsgericht Charlottenburg. Registernummer: HRB 123456 B</p>
<h2>Umsatzsteuer</h2><p>Umsatzsteuer-Identifikationsnummer: DE123456789</p>
"""

DISCLAIMER = (
    "<h2>Haftung für Inhalte</h2><p>"
    + "Als Diensteanbieter sind wir für eigene Inhalte auf diesen Seiten nach den "
      "allgemeinen Gesetzen verantwortlich. " * 40
    + "</p><h2>Streitschlichtung</h2><p>"
    + "Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung bereit. " * 20
    + "</p>")


def menu(n=60):
    return "".join(f"<li><a href='/p{i}'>Produkt Kategorie {i} Nahrungsergänzung</a></li>"
                   for i in range(n))


def footer_links(n=80):
    return "".join(f"<li><a href='/f{i}'>Footer Link Nummer {i}</a></li>" for i in range(n))


def about(repeat=40):
    return "<p>" + "Wir sind ein junges Unternehmen mit hochwertigen Produkten. " * repeat + "</p>"


def bloated_page(legal=LEGAL_BLOCK, title="Impressum | Muster"):
    """A realistic bloated Impressum page: menu, cookie banner, long about text, the legal
    block, long disclaimers, a huge footer and scripts."""
    return f"""<html><head><title>{title}</title><script>var x='{"a" * 3000}';</script></head>
<body><div class='cookie'>Wir verwenden Cookies, um die Nutzerfreundlichkeit zu verbessern. Alle akzeptieren</div>
<header><nav><ul>{menu()}</ul></nav></header>
<main><div>{about()}{legal}{DISCLAIMER}</div></main>
<footer><ul>{footer_links()}</ul><p>© 2024 Muster Nutrition GmbH</p><a href='/datenschutz'>Datenschutz</a></footer>
</body></html>"""
