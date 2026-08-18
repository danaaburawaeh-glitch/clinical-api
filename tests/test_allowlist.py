"""Allowlist and URL-validation tests (PART 20, PART 47).

These are the tests that matter most: if any of them regress, the
"the model cannot reach an unapproved source" guarantee is gone.
"""

from __future__ import annotations

import pytest

from app.security.allowlist import SourceCategory, SourceRegistry, TrustTier
from app.security.url_validator import (
    UrlValidationError,
    is_allowed_url,
    validate_url_sync,
)
from app.utils.normalize import normalize_hostname


# ----------------------------------------------------------------------
# Hostname normalisation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://pubmed.ncbi.nlm.nih.gov/123/", "pubmed.ncbi.nlm.nih.gov"),
        ("https://PubMed.NCBI.NLM.NIH.GOV/123/", "pubmed.ncbi.nlm.nih.gov"),
        ("https://pubmed.ncbi.nlm.nih.gov./123/", "pubmed.ncbi.nlm.nih.gov"),
        ("pubmed.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"),
        ("https://user:pass@ivoclar.com/x", "ivoclar.com"),
        # Userinfo trick: the real host is evil.com, not the approved one.
        ("https://pubmed.ncbi.nlm.nih.gov@evil.com/x", "evil.com"),
        ("", ""),
        ("not a url at all", ""),
    ],
)
def test_normalize_hostname(raw, expected):
    assert normalize_hostname(raw) == expected


def test_normalize_hostname_rejects_percent_encoded_host():
    # %2e is '.', an old trick for smuggling a different authority.
    assert normalize_hostname("https://ivoclar%2ecom.evil.com/") == ""


# ----------------------------------------------------------------------
# Domain matching
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://pubmed.ncbi.nlm.nih.gov/34567890/",
        "https://europepmc.org/article/MED/123",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        "https://api.crossref.org/works/10.1000/x",
        "https://api.fda.gov/device/510k.json",
        "https://accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm",
        "https://www.ivoclar.com/en/p/all/products",
        "https://sfda.gov.sa/en/medicaldevices",
        "https://www.ada.org/resources/research",
    ],
)
def test_approved_urls_are_allowed(url, registry):
    assert is_allowed_url(url, registry) is True


@pytest.mark.parametrize(
    "url",
    [
        # Suffix-confusion attacks
        "https://ivoclar.com.evil.com/ifu.pdf",
        "https://pubmed.ncbi.nlm.nih.gov.evil.org/34567890/",
        "https://fda.gov.attacker.net/device",
        # Prefix-confusion attacks
        "https://notivoclar.com/x",
        "https://fakepubmed.com/x",
        "https://xivoclar.com/x",
        # Plain unapproved sources
        "https://randomdentalblog.com/best-veneers",
        "https://www.reddit.com/r/Dentistry",
        "https://dental-distributor-shop.com/emax",
        # Subdomain of a domain explicitly configured include_subdomains: false
        "https://evil.pubmed.ncbi.nlm.nih.gov.attacker.io/",
    ],
)
def test_unapproved_urls_are_blocked(url, registry):
    assert is_allowed_url(url, registry) is False


def test_include_subdomains_false_is_respected():
    registry = SourceRegistry.from_mapping(
        {
            "strict": {
                "domain": "example-journal.org",
                "include_subdomains": False,
                "category": "journal",
                "trust_tier": "evidence",
            }
        }
    )
    assert registry.is_allowed_host("example-journal.org") is True
    assert registry.is_allowed_host("sub.example-journal.org") is False


def test_include_subdomains_true_is_respected():
    registry = SourceRegistry.from_mapping(
        {
            "loose": {
                "domain": "example-org.org",
                "include_subdomains": True,
                "category": "regulator",
                "trust_tier": "regulatory",
            }
        }
    )
    assert registry.is_allowed_host("example-org.org") is True
    assert registry.is_allowed_host("deep.sub.example-org.org") is True
    assert registry.is_allowed_host("example-org.org.evil.net") is False
    assert registry.is_allowed_host("notexample-org.org") is False


# ----------------------------------------------------------------------
# Scheme handling
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://pubmed.ncbi.nlm.nih.gov/x",
        "gopher://pubmed.ncbi.nlm.nih.gov/x",
        "data:text/html;base64,PHNjcmlwdD4=",
        "javascript:alert(1)",
        "dict://pubmed.ncbi.nlm.nih.gov:11211/stat",
        "ldap://pubmed.ncbi.nlm.nih.gov/",
        "http://pubmed.ncbi.nlm.nih.gov/123/",  # https-only by default
    ],
)
def test_dangerous_schemes_rejected(url, registry):
    with pytest.raises(UrlValidationError):
        validate_url_sync(url, registry=registry)


def test_url_without_scheme_rejected(registry):
    with pytest.raises(UrlValidationError):
        validate_url_sync("pubmed.ncbi.nlm.nih.gov/123", registry=registry)


def test_userinfo_rejected(registry):
    with pytest.raises(UrlValidationError, match="userinfo"):
        validate_url_sync(
            "https://pubmed.ncbi.nlm.nih.gov@evil.com/x", registry=registry
        )


def test_control_characters_rejected(registry):
    with pytest.raises(UrlValidationError, match="control characters"):
        validate_url_sync(
            "https://pubmed.ncbi.nlm.nih.gov/\r\nHost: evil.com", registry=registry
        )


def test_unusual_port_rejected(registry):
    with pytest.raises(UrlValidationError, match="port"):
        validate_url_sync("https://pubmed.ncbi.nlm.nih.gov:8443/x", registry=registry)


def test_overlong_url_rejected(registry):
    long_url = "https://pubmed.ncbi.nlm.nih.gov/" + "a" * 3000
    with pytest.raises(UrlValidationError, match="maximum length"):
        validate_url_sync(long_url, registry=registry)


# ----------------------------------------------------------------------
# Categories, tiers and roles
# ----------------------------------------------------------------------
def test_manufacturer_category_and_forbidden_uses(registry):
    entry = registry.match_host("ivoclar.com")
    assert entry is not None
    assert entry.category == SourceCategory.MANUFACTURER
    assert entry.trust_tier == TrustTier.MANUFACTURER
    assert "proving_clinical_superiority" in entry.forbidden_for
    assert "IFU" in entry.allowed_for


def test_pubmed_is_evidence_tier(registry):
    entry = registry.match_host("pubmed.ncbi.nlm.nih.gov")
    assert entry is not None
    assert entry.category == SourceCategory.SCIENTIFIC_DATABASE
    assert entry.trust_tier == TrustTier.EVIDENCE
    assert entry.forbidden_for == ()


def test_crossref_forbidden_for_clinical_recommendation(registry):
    entry = registry.match_host("api.crossref.org")
    assert entry is not None
    assert entry.category == SourceCategory.BIBLIOGRAPHIC_METADATA
    assert "clinical_recommendation" in entry.forbidden_for


def test_required_category_is_enforced(registry):
    # An evidence domain must be refused when a manufacturer is required.
    with pytest.raises(UrlValidationError, match="category"):
        validate_url_sync(
            "https://pubmed.ncbi.nlm.nih.gov/1/",
            registry=registry,
            required_category=SourceCategory.MANUFACTURER,
        )
    # And the matching one must pass.
    validated = validate_url_sync(
        "https://www.ivoclar.com/en/x",
        registry=registry,
        required_category=SourceCategory.MANUFACTURER,
    )
    assert validated.entry.domain == "ivoclar.com"


def test_manufacturer_resolution_by_name(registry):
    assert registry.find_manufacturer("Ivoclar Vivadent").domain == "ivoclar.com"
    assert registry.find_manufacturer("Kuraray Noritake").domain == "kuraraynoritake.com"
    assert registry.find_manufacturer("3Shape").domain == "3shape.com"
    assert registry.find_manufacturer("Some Random Distributor") is None


def test_describe_unapproved_domain(registry):
    described = registry.describe("randomdentalblog.com")
    assert described["allowed"] is False
    assert described["source_category"] == "unapproved"
    assert described["allowed_use"] == "none"


def test_describe_manufacturer_domain(registry):
    described = registry.describe("https://www.ivoclar.com/en/x")
    assert described["allowed"] is True
    assert described["source_category"] == "manufacturer"
    assert described["allowed_use"] == "IFU and product technical information only"


def test_registry_skips_malformed_entries():
    registry = SourceRegistry.from_mapping(
        {
            "no_domain": {"category": "journal", "trust_tier": "evidence"},
            "bad_category": {"domain": "x.org", "category": "nonsense"},
            "not_a_mapping": "hello",
            "good": {
                "domain": "good-example.org",
                "category": "journal",
                "trust_tier": "evidence",
            },
        }
    )
    assert registry.all_domains() == ["good-example.org"]


def test_every_configured_domain_has_a_known_category(registry):
    for entry in registry.entries.values():
        assert entry.category in SourceCategory.ALL
        assert entry.trust_tier in {
            TrustTier.EVIDENCE,
            TrustTier.GUIDELINE,
            TrustTier.PROFESSIONAL,
            TrustTier.REGULATORY,
            TrustTier.MANUFACTURER,
            TrustTier.METADATA,
        }


def test_all_manufacturers_forbid_superiority_claims(registry):
    for entry in registry.manufacturers():
        assert "proving_clinical_superiority" in entry.forbidden_for, entry.domain
