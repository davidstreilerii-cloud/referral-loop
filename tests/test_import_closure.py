"""Success criterion 6: the referral install needs neither ChromaDB nor the corpus."""
import importlib
import sys

FORBIDDEN = {
    "chromadb", "sentence_transformers", "torch", "transformers",
    "healthcare_rag.revenue_integrity", "healthcare_rag.denial_rca",
    "healthcare_rag.db", "healthcare_rag.audit_trail",
    "healthcare_rag.guardrails.tenant_isolation",
}

REFERRAL_MODULES = [
    "healthcare_rag.referral_loop",
    "healthcare_rag.referral_loop.errors",
]


def test_referral_modules_import_no_forbidden_dependency():
    for name in list(sys.modules):
        if name.startswith("healthcare_rag.referral_loop"):
            del sys.modules[name]

    before = set(sys.modules)
    for mod in REFERRAL_MODULES:
        importlib.import_module(mod)
    newly_imported = set(sys.modules) - before

    leaked = {m for m in newly_imported if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}
    assert leaked == set(), f"referral_loop pulled in forbidden modules: {sorted(leaked)}"
