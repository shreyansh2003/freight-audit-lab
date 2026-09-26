"""Audit and accrual code needs pricing but must never depend on the data generator.

Pricing lives in freight_audit_lab/contract.py. Only generate/ itself and run.py (which
launches the generator) may import freight_audit_lab.generate.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "freight_audit_lab"
ALLOWED = {"run.py"}          # plus everything under generate/


def imports_generate(source, module_name="freight_audit_lab.somemodule"):
    """True if the source imports freight_audit_lab.generate in any spelling."""
    package = module_name.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: resolve against this module's package
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([base] if base else []))
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        else:
            continue
        if any(n == "freight_audit_lab.generate" or n.startswith("freight_audit_lab.generate.")
               for n in names):
            return True
    return False


def test_detector_catches_every_import_spelling():
    for src in ["import freight_audit_lab.generate",
                "import freight_audit_lab.generate.rates as r",
                "from freight_audit_lab.generate import rates",
                "from freight_audit_lab.generate.rates import lookup_rate",
                "from freight_audit_lab import generate"]:
        assert imports_generate(src), src
    assert imports_generate("from .generate import rates", "freight_audit_lab.audit_thing")
    assert imports_generate("from ..generate import rates", "freight_audit_lab.audit.rules")
    assert not imports_generate("from freight_audit_lab.contract import lookup_rate")
    assert not imports_generate("from freight_audit_lab import config")


def test_nothing_outside_generate_imports_generate():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts[0] == "generate" or str(rel) in ALLOWED:
            continue
        module = "freight_audit_lab." + ".".join(rel.with_suffix("").parts)
        if imports_generate(path.read_text(), module):
            offenders.append(str(rel))
    assert not offenders, f"these modules must not import freight_audit_lab.generate: {offenders}"
