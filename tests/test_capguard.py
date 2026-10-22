"""Tool-exposure boundary of the ITSM MCP server per principal, checked with mcp-capguard."""

import pytest
from mcp_capguard import CapabilityProfile, CapabilityViolation

from arp.tools.itsm import ITSM
from arp.tools.kb import KnowledgeBase
from arp.tools.server import READ, READ_TOOLS, WRITE, WRITE_TOOLS, Principal, build_server


def factory(principal):
    return build_server(principal, ITSM(), KnowledgeBase())


PROFILES = [
    CapabilityProfile(
        name="agent-readonly",
        factory=factory,
        settings=Principal("acme", "agent:ro"),
        must_expose=frozenset(READ_TOOLS),
        must_not_expose=frozenset(WRITE_TOOLS),
        strict=True,
    ),
    CapabilityProfile(
        name="agent-operator",
        factory=factory,
        settings=Principal("acme", "agent:op", frozenset({READ, WRITE})),
        must_expose=frozenset(READ_TOOLS + WRITE_TOOLS),
        strict=True,
    ),
]


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_profiles(profile):
    profile.check_sync()


def test_negative_control_write_scope_leaks_into_readonly_policy():
    leaky = CapabilityProfile(
        name="leak",
        factory=factory,
        settings=Principal("acme", "x", frozenset({READ, WRITE})),
        must_expose=frozenset(READ_TOOLS),
        must_not_expose=frozenset(WRITE_TOOLS),
    )
    with pytest.raises(CapabilityViolation):
        leaky.check_sync()
