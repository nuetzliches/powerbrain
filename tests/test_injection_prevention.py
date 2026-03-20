"""
Tests für P1-2 (SQL-Injection) und P1-3 (Cypher-Injection) Prevention.

Prüft, dass:
1. server.py condition-keys validiert bevor sie in SQL eingebaut werden
2. graph_service.py labels und property-keys validiert
3. Eine gemeinsame validate_identifier-Funktion korrekt arbeitet
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# ── Hilfsfunktion: importiere validate_identifier aus graph_service ──

def _load_validate_identifier():
    """
    Lädt die validate_identifier Funktion aus graph_service.py
    ohne den ganzen Module-Import (vermeidet asyncpg-Abhängigkeit).
    """
    source = (ROOT / "mcp-server" / "graph_service.py").read_text()
    # Funktion muss im Source existieren
    if "def validate_identifier" not in source:
        return None

    # Extrahiere die Funktion und zugehörige Konstanten
    lines = source.split("\n")
    extracted_lines = []

    # Sammle Konstanten die die Funktion braucht
    for line in lines:
        if line.startswith("_IDENTIFIER_RE"):
            extracted_lines.append(line)

    # Sammle die Funktion selbst
    in_func = False
    for line in lines:
        if "def validate_identifier" in line:
            in_func = True
            extracted_lines.append(line)
        elif in_func:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            extracted_lines.append(line)

    if not extracted_lines:
        return None

    ns = {"re": re}
    exec("\n".join(extracted_lines), ns)
    return ns.get("validate_identifier")


class TestValidateIdentifier(unittest.TestCase):
    """Unit-Tests für die Identifier-Validierungsfunktion."""

    @classmethod
    def setUpClass(cls):
        fn = _load_validate_identifier()
        cls.validate = staticmethod(fn) if fn else None

    def test_validate_identifier_function_exists(self):
        """validate_identifier muss in graph_service.py existieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        self.assertIn("validate_identifier", source,
                       "graph_service.py muss eine validate_identifier Funktion haben")

    def test_accepts_simple_alpha_keys(self):
        """Normale Identifier wie 'name', 'status' müssen akzeptiert werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        for key in ("name", "status", "project_id", "myField", "a"):
            self.assertTrue(
                self.validate(key),
                f"Valider Identifier '{key}' wurde abgelehnt"
            )

    def test_accepts_underscore_prefix(self):
        """Identifier mit Unterstrich-Prefix wie '_id' müssen akzeptiert werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertTrue(self.validate("_id"))
        self.assertTrue(self.validate("_internal_field"))

    def test_accepts_alphanumeric_with_underscore(self):
        """Alphanumerische Identifier mit Unterstrichen sind gültig."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertTrue(self.validate("field_2"))
        self.assertTrue(self.validate("my_field_name"))
        self.assertTrue(self.validate("A1B2"))

    def test_rejects_sql_injection_single_quote(self):
        """Keys mit SQL-Injection-Versuchen (Single Quote) müssen abgelehnt werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        malicious = "'; DROP TABLE datasets; --"
        self.assertFalse(
            self.validate(malicious),
            f"SQL-Injection-Key wurde nicht abgelehnt: {malicious}"
        )

    def test_rejects_sql_injection_double_dash(self):
        """Keys mit SQL-Kommentar (--) müssen abgelehnt werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertFalse(self.validate("key--comment"))

    def test_rejects_cypher_injection_curly_braces(self):
        """Keys mit Cypher-Injection (geschweifte Klammern) müssen abgelehnt werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        malicious = "name}}) RETURN n UNION MATCH (m) DETACH DELETE m //"
        self.assertFalse(
            self.validate(malicious),
            f"Cypher-Injection-Key wurde nicht abgelehnt: {malicious}"
        )

    def test_rejects_spaces(self):
        """Keys mit Leerzeichen müssen abgelehnt werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertFalse(self.validate("key with spaces"))

    def test_rejects_special_characters(self):
        """Keys mit Sonderzeichen müssen abgelehnt werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        for char in (".", ",", ";", "(", ")", "[", "]", "{", "}", "'", '"',
                      "=", "<", ">", "/", "\\", "$", "@", "!", "&", "|"):
            malicious = f"key{char}injection"
            self.assertFalse(
                self.validate(malicious),
                f"Key mit Sonderzeichen wurde nicht abgelehnt: {malicious}"
            )

    def test_rejects_empty_string(self):
        """Leere Strings sind keine gültigen Identifier."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertFalse(self.validate(""))

    def test_rejects_numeric_prefix(self):
        """Identifier dürfen nicht mit einer Zahl beginnen."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertFalse(self.validate("1field"))
        self.assertFalse(self.validate("123"))

    def test_rejects_unicode_bypass(self):
        """Unicode-Zeichen dürfen nicht als Bypass verwendet werden."""
        if not self.validate:
            self.skipTest("validate_identifier nicht gefunden")
        self.assertFalse(self.validate("naïve"))
        self.assertFalse(self.validate("tëst"))


class TestSQLInjectionPrevention(unittest.TestCase):
    """P1-2: Strukturelle Tests — server.py validiert query_data condition keys."""

    def test_query_data_validates_condition_keys(self):
        """
        server.py query_data handler muss condition keys validieren
        bevor sie in SQL-Strings interpoliert werden.
        """
        source = (ROOT / "mcp-server" / "server.py").read_text()

        # Der query_data Block muss eine Validierung der Keys enthalten.
        # Suche im _dispatch-Abschnitt nach query_data
        dispatch_start = source.index("async def _dispatch")
        query_data_start = source.index('name == "query_data"', dispatch_start)
        next_handler = source.find("\n    elif name ==", query_data_start + 1)
        if next_handler > 0:
            query_data_section = source[query_data_start:next_handler]
        else:
            query_data_section = source[query_data_start:]

        has_validation = (
            "validate_identifier" in query_data_section
            or "_require_identifier" in query_data_section
        )
        self.assertTrue(has_validation,
                        "query_data handler muss condition keys mit validate_identifier/_require_identifier prüfen")

    def test_no_raw_key_interpolation_in_query_data(self):
        """
        server.py darf condition keys nicht direkt via f-string in SQL einbauen
        ohne vorherige Validierung.
        """
        source = (ROOT / "mcp-server" / "server.py").read_text()

        # Finde den query_data Abschnitt im _dispatch
        dispatch_start = source.index("async def _dispatch")
        start = source.index('name == "query_data"', dispatch_start)
        next_elif = source.index("\n    elif", start + 1)
        query_data_section = source[start:next_elif]

        # Der key muss validiert werden BEVOR er in den SQL-String kommt.
        validate_pos = max(
            query_data_section.find("validate_identifier"),
            query_data_section.find("_require_identifier"),
        )
        fstring_pos = query_data_section.find("data->>'")

        self.assertGreater(validate_pos, -1,
                           "validate_identifier/_require_identifier muss im query_data Block aufgerufen werden")
        # Wenn f-string noch da ist, muss validate davor kommen
        if fstring_pos > -1:
            self.assertLess(validate_pos, fstring_pos,
                            "Validierung muss VOR der SQL-Key-Interpolation stehen")


def _has_identifier_validation(text: str) -> bool:
    """Prüft ob text validate_identifier oder _require_identifier enthält."""
    return "validate_identifier" in text or "_require_identifier" in text


class TestCypherInjectionPrevention(unittest.TestCase):
    """P1-3: Strukturelle Tests — graph_service.py validiert labels und property keys."""

    def test_graph_service_imports_or_defines_validate_identifier(self):
        """graph_service.py muss validate_identifier definieren oder importieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        self.assertTrue(_has_identifier_validation(source),
                        "graph_service.py muss validate_identifier/_require_identifier haben")

    def test_create_node_validates_label(self):
        """create_node muss das Label validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def create_node")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body),
                        "create_node muss label mit validate_identifier/_require_identifier prüfen")

    def test_create_node_validates_property_keys(self):
        """create_node muss property keys validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def create_node")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_find_node_validates_label(self):
        """find_node muss das Label validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def find_node")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body),
                        "find_node muss label mit validate_identifier/_require_identifier prüfen")

    def test_find_node_validates_property_keys(self):
        """find_node muss property keys validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def find_node")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_delete_node_validates_label(self):
        """delete_node muss das Label validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def delete_node")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_create_relationship_validates_labels_and_rel_type(self):
        """create_relationship muss alle Labels und rel_type validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def create_relationship")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_get_neighbors_validates_label(self):
        """get_neighbors muss das Label validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def get_neighbors")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_find_path_validates_labels(self):
        """find_path muss from_label und to_label validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def find_path")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))

    def test_find_relationships_validates_labels_and_rel_type(self):
        """find_relationships muss Labels und rel_type validieren."""
        source = (ROOT / "mcp-server" / "graph_service.py").read_text()
        start = source.index("async def find_relationships")
        next_func = source.index("\nasync def ", start + 1)
        func_body = source[start:next_func]

        self.assertTrue(_has_identifier_validation(func_body))


if __name__ == "__main__":
    unittest.main()
