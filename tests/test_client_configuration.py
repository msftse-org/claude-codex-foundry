"""Provider projection shared by installer exports and future refresh."""

import copy

import pytest

from claude_foundry.portal.client_configuration import project_profile
from claude_foundry.portal.domain import seed

OWNER = "00000000-0000-0000-0000-000000000003"


@pytest.fixture
def policy(org):
    return seed(org, OWNER, "Synthetic owner", "owner@example.com")


def test_projection_intersects_catalog_and_effective_grants_without_mutation(org, policy):
    config = copy.deepcopy(org)
    config["models"].extend(
        [
            {
                "name": "claude-personal",
                "anthropicFamilyTier": "sonnet",
                "isFamilyDefault": False,
            },
            {"name": "claude-not-granted", "anthropicFamilyTier": "opus"},
        ]
    )
    member = policy["members"][0]
    member["extra_models"] = ["claude-personal", "not-in-provider"]
    before = copy.deepcopy((config, policy))
    result = project_profile(config, policy, member)
    assert [m["name"] for m in result["models"]] == [
        "claude-sonnet-4-6",
        "claude-personal",
    ]
    result["models"][0]["labelOverride"] = "Local mutation"
    assert (config, policy) == before


def test_group_export_excludes_personal_grants(org, policy):
    config = {
        **org,
        "models": [
            *org["models"],
            {"name": "claude-personal", "anthropicFamilyTier": "opus"},
        ],
    }
    member = policy["members"][0]
    member["extra_models"] = ["claude-personal"]
    result = project_profile(config, policy, member, group_id=member["group_id"])
    assert [m["name"] for m in result["models"]] == ["claude-sonnet-4-6"]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_projection_can_represent_no_models(org, policy, client):
    member = policy["members"][0]
    policy["groups"][0].update(model_ids=[], default_model="")
    config = {**org, "client_type": client}
    assert project_profile(config, policy, member)["models"] == []
    # A suspended member's personal grants do not restore access.
    member.update(status="suspended", extra_models=["claude-sonnet-4-6"])
    assert project_profile(config, policy, member)["models"] == []


def test_claude_defaults_are_scoped_and_each_remaining_family_has_a_default(org, policy):
    config = {
        **org,
        "models": [
            *org["models"],
            {"name": "claude-new", "anthropicFamilyTier": "sonnet", "isFamilyDefault": False},
            {"name": "claude-opus", "anthropicFamilyTier": "opus"},
        ],
    }
    policy["groups"][0].update(model_ids=["claude-new", "claude-opus"], default_model="claude-new")
    result = project_profile(config, policy, policy["members"][0])
    assert result["models"] == [
        {"name": "claude-new", "anthropicFamilyTier": "sonnet", "isFamilyDefault": True},
        {"name": "claude-opus", "anthropicFamilyTier": "opus", "isFamilyDefault": True},
    ]


@pytest.mark.parametrize(
    ("group_default", "provider_default", "expected"),
    [
        ("gpt-second", "gpt-first", "gpt-second"),
        ("claude-sonnet-4-6", "gpt-second", "gpt-second"),
        ("claude-sonnet-4-6", "not-granted", "gpt-first"),
    ],
)
def test_codex_default_never_points_to_another_provider(
    org, policy, group_default, provider_default, expected
):
    config = {
        **org,
        "client_type": "codex",
        "default_model": provider_default,
        "models": [
            {"name": "gpt-first", "model": "gpt-first", "qualified": True},
            {"name": "gpt-second", "model": "gpt-second", "qualified": True},
        ],
    }
    policy["groups"][0].update(
        model_ids=["claude-sonnet-4-6", "gpt-first", "gpt-second"],
        default_model=group_default,
    )
    result = project_profile(config, policy, policy["members"][0])
    assert {m["name"] for m in result["models"]} == {"gpt-first", "gpt-second"}
    assert result["default_model"] == expected
