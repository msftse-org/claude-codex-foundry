import base64
import copy
import json

import pytest
import tomlkit
from test_configuration_client import (
    OTHER,
    SUBJECT,
    body_and_etag,
    configuration_value,
    descriptor_value,
)

from claude_foundry import cli, local_update
from claude_foundry import configuration_refresh as refresh
from claude_foundry.configuration_client import (
    AccessUnavailable,
    ConfigurationError,
    EnrollmentDescriptor,
    SignInRequired,
    VerifiedConfiguration,
    _validate,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(value if isinstance(value, bytes) else refresh._json(value))
    path.chmod(0o600)


def read(path):
    return json.loads(path.read_bytes())


def verified(value=None):
    body, etag = body_and_etag(value or configuration_value())
    return _validate(body, etag, EnrollmentDescriptor.parse(descriptor_value()), SUBJECT)


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    # Every settings mutation is inside a disposable client home.
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    descriptor = tmp_path / "organization.json"
    write(descriptor, descriptor_value())
    executable = tmp_path / "claude-foundry"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    native = {
        "models": [
            {
                "slug": "gpt-test",
                "base_instructions": "Native client instructions retained.",
                "supported_reasoning_levels": [],
            }
        ]
    }
    monkeypatch.setattr(refresh.codex_client, "bundled_catalog", lambda _: native)
    calls = []
    payload = base64.urlsafe_b64encode(json.dumps({"oid": SUBJECT}).encode()).decode().rstrip("=")
    login_token = f"header.{payload}.signature"

    class Auth:
        def __init__(self, selected, directory, subject_id=None):
            calls.append(("auth", selected, directory, subject_id))

        def login(self):
            calls.append(("login",))
            return login_token

        def configuration_token(self):
            calls.append(("configuration-token",))
            return "configuration-credential"

        def inference_token(self, *, interactive=False):
            calls.append(("inference-token", interactive))
            return "inference-credential"

    monkeypatch.setattr(refresh, "EnrolledMsal", Auth)
    monkeypatch.setattr(refresh, "fetch_initial_configuration", lambda *_: verified())

    def fetch(selected, subject, token, *, previous):
        assert selected == EnrollmentDescriptor.parse(descriptor_value())
        assert subject == SUBJECT and token in {"configuration-credential", login_token}
        assert previous == verified()
        return verified()

    monkeypatch.setattr(refresh, "fetch_configuration", fetch)
    paths = refresh._paths(home, EnrollmentDescriptor.parse(descriptor_value()))

    def enroll(**kwargs):
        return refresh.enroll(
            descriptor, home=home, executable=executable, codex_binary=executable, **kwargs
        )

    return home, descriptor, paths, calls, enroll


def settings(paths):
    return {key: path.read_bytes() for key, path in paths.items() if path.exists()}


def test_initial_enrollment_pins_identity_routes_helpers_and_private_state(pilot):
    home, _, paths, calls, enroll = pilot
    result = enroll()
    assert result["models"] == {"claude": 1, "codex": 1}
    binding = read(paths["binding"])
    assert binding["subject_id"] == SUBJECT
    assert binding["descriptor"] == descriptor_value()
    assert [call[0] for call in calls].count("login") == 1
    assert ("inference-token", True) in calls
    assert all(
        call[2] == home / refresh.STATE / "credentials" for call in calls if call[0] == "auth"
    )
    assert read(paths["claude_cli"])["env"]["ANTHROPIC_BASE_URL"].endswith("/claude")
    desktop = read(paths["claude_profile"])
    assert desktop["inferenceGatewayBaseUrl"].endswith("/claude")
    assert desktop["inferenceCredentialHelper"] == str(paths["helper"])
    assert read(paths["claude_meta"])["appliedId"] == paths["claude_profile"].stem
    codex = tomlkit.parse(paths["codex"].read_text()).unwrap()
    assert codex["model_catalog_json"] == str(paths["catalog"])
    assert codex["model_providers"]["azure-foundry"]["base_url"].endswith("/codex/v1")
    catalog = read(paths["catalog"])
    assert catalog["models"][0]["slug"] == "coding-example"
    assert catalog["models"][0]["base_instructions"] == "Native client instructions retained."
    assert b"enrolled-token" in paths["helper"].read_bytes()
    for key in ("binding", "ownership", "cache", "status", "catalog"):
        assert paths[key].stat().st_mode & 0o777 == 0o600
    assert paths["helper"].stat().st_mode & 0o777 == 0o700
    assert "subject" not in json.dumps(refresh.status(home=home))


def test_conditional_refresh_never_logs_in_or_changes_routing(pilot):
    home, _, paths, calls, enroll = pilot
    enroll()
    before = settings(paths)
    calls.clear()
    refresh.refresh(home=home)
    assert ("login",) not in calls and ("configuration-token",) in calls
    assert all(call[3] == SUBJECT for call in calls if call[0] == "auth")
    after = settings(paths)
    for key in before.keys() - {"status"}:
        assert after[key] == before[key]


def test_explicit_reauthentication_checks_both_audiences_before_applying(pilot):
    home, _, paths, calls, enroll = pilot
    enroll()
    before = settings(paths)
    calls.clear()
    refresh.refresh(home=home, login=True)
    assert ("login",) in calls
    assert ("inference-token", True) in calls
    assert ("configuration-token",) not in calls
    assert all(call[3] == SUBJECT for call in calls if call[0] == "auth")
    for key in before.keys() - {"status"}:
        assert settings(paths)[key] == before[key]


@pytest.mark.parametrize("operation", ["enroll", "refresh"])
def test_inference_key_store_failure_never_replaces_client_settings(pilot, monkeypatch, operation):
    home, _, paths, _, enroll = pilot
    if operation == "refresh":
        enroll()
    before = settings(paths)

    def fail(*_, **__):
        raise SignInRequired("Sign-in required")

    monkeypatch.setattr(refresh.EnrolledMsal, "inference_token", fail)
    with pytest.raises(SignInRequired):
        enroll() if operation == "enroll" else refresh.refresh(home=home, login=True)
    assert settings(paths) == before


def test_publish_new_models_updates_both_pickers_and_preserves_user_preferences(pilot, monkeypatch):
    home, _, paths, _, enroll = pilot
    write(paths["claude_cli"], {"theme": "dark", "env": {"CUSTOM": "keep"}})
    write(paths["codex"], b'# my comment\npersonality = "pragmatic"\n')
    enroll()
    before_helper = paths["helper"].read_bytes()
    value = configuration_value()
    value["clients"]["claude"]["models"].append(
        {"name": "claude-opus-new", "family_tier": "opus", "family_default": True}
    )
    value["clients"]["codex"]["models"].append({"name": "coding-new", "model": "gpt-test"})
    monkeypatch.setattr(refresh, "fetch_configuration", lambda *_, **__: verified(value))
    result = refresh.refresh(home=home)
    assert result["models"] == {"claude": 2, "codex": 2}
    current = read(paths["claude_cli"])
    assert current["theme"] == "dark" and current["env"]["CUSTOM"] == "keep"
    assert current["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-new"
    assert current["model"] == "claude-sonnet-example"
    assert len(json.loads(read(paths["claude_profile"])["inferenceModels"])) == 2
    assert [m["slug"] for m in read(paths["catalog"])["models"]] == ["coding-example", "coding-new"]
    assert "# my comment" in paths["codex"].read_text()
    assert tomlkit.parse(paths["codex"].read_text())["personality"] == "pragmatic"
    assert paths["helper"].read_bytes() == before_helper


def test_removed_selection_moves_to_published_default_and_owned_alias_is_removed(
    pilot, monkeypatch
):
    home, _, paths, _, enroll = pilot
    enroll()
    value = configuration_value()
    value["clients"]["claude"].update(
        default_model="claude-opus-new",
        models=[{"name": "claude-opus-new", "family_tier": "opus", "family_default": True}],
    )
    value["clients"]["codex"].update(
        default_model="coding-new", models=[{"name": "coding-new", "model": "gpt-test"}]
    )
    monkeypatch.setattr(refresh, "fetch_configuration", lambda *_, **__: verified(value))
    refresh.refresh(home=home)
    current = read(paths["claude_cli"])
    assert current["model"] == "claude-opus-new"
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in current["env"]
    assert tomlkit.parse(paths["codex"].read_text())["model"] == "coding-new"


def test_manual_alias_and_desktop_preferences_are_not_reclaimed(pilot):
    home, _, paths, _, enroll = pilot
    enroll()
    current = read(paths["claude_cli"])
    current["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "user-choice"
    write(paths["claude_cli"], current)
    desktop = read(paths["claude_profile"])
    desktop["claudeAiImport"] = {"enabled": False}
    desktop["customPreference"] = "keep"
    write(paths["claude_profile"], desktop)
    refresh.refresh(home=home)
    assert read(paths["claude_cli"])["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "user-choice"
    assert read(paths["claude_profile"])["claudeAiImport"] == {"enabled": False}
    assert read(paths["claude_profile"])["customPreference"] == "keep"
    assert not read(paths["ownership"])["installed"]["claude_aliases"]


@pytest.mark.parametrize(
    "error",
    [
        SignInRequired("Sign in required"),
        AccessUnavailable("Access revoked"),
        ConfigurationError("Network unavailable"),
    ],
)
def test_auth_revocation_and_offline_errors_preserve_last_successful_settings(
    pilot, monkeypatch, error
):
    home, _, paths, calls, enroll = pilot
    enroll()
    before = settings(paths)
    calls.clear()

    def fail(*_, **__):
        raise error

    monkeypatch.setattr(refresh, "fetch_configuration", fail)
    with pytest.raises(type(error)):
        refresh.refresh(home=home)
    assert settings(paths) == before
    assert ("login",) not in calls


def test_no_models_removes_catalog_entries_but_keeps_credentials_server_authoritative(
    pilot, monkeypatch
):
    home, _, paths, _, enroll = pilot
    enroll()
    value = configuration_value()
    value["state"] = "no_models"
    for entry in value["clients"].values():
        entry.update(default_model="", models=[])
    monkeypatch.setattr(refresh, "fetch_configuration", lambda *_, **__: verified(value))
    assert refresh.refresh(home=home)["state"] == "no_models"
    assert read(paths["catalog"]) == {"models": []}
    assert json.loads(read(paths["claude_profile"])["inferenceModels"]) == []
    assert refresh.inference_token(home=home) == "inference-credential"
    # Cached models are never consulted to manufacture authorization.


@pytest.mark.parametrize("target", ["claude_cli", "claude_profile", "codex", "catalog", "helper"])
def test_routing_helper_and_catalog_drift_stops_without_overwriting(pilot, target):
    home, _, paths, _, enroll = pilot
    enroll()
    if target == "claude_cli":
        value = read(paths[target])
        value["env"]["ANTHROPIC_BASE_URL"] = "https://other.example.com"
        write(paths[target], value)
    elif target == "claude_profile":
        value = read(paths[target])
        value["inferenceCredentialHelper"] = "/changed/helper"
        write(paths[target], value)
    elif target == "codex":
        value = tomlkit.parse(paths[target].read_text())
        value["model_providers"]["azure-foundry"]["base_url"] = "https://other.example.com"
        write(paths[target], tomlkit.dumps(value).encode())
    elif target == "catalog":
        write(paths[target], {"models": []})
    else:
        paths[target].write_bytes(b"#!/bin/sh\nexit 1\n")
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="changed locally"):
        refresh.refresh(home=home)
    assert settings(paths) == before


def test_interrupted_multi_client_update_rolls_back_every_file(pilot, monkeypatch):
    home, _, paths, _, enroll = pilot
    original = local_update._replace
    failed = False

    def interrupt(path, value):
        nonlocal failed
        if path == paths["codex"] and not failed:
            failed = True
            raise OSError("simulated write failure")
        return original(path, value)

    monkeypatch.setattr(local_update, "_replace", interrupt)
    with pytest.raises(OSError, match="simulated"):
        enroll()
    assert settings(paths) == {}
    assert not (home / refresh.STATE / "pending-update.json").exists()
    enroll()
    assert read(paths["binding"])["subject_id"] == SUBJECT


@pytest.mark.parametrize("field", ["subject_id", "tenant_id", "organization_id"])
def test_wrong_bound_feed_never_reaches_disk(pilot, monkeypatch, field):
    home, _, paths, _, enroll = pilot
    enroll()
    before = settings(paths)
    value = configuration_value()
    value[field] = OTHER
    body, etag = body_and_etag(value)
    monkeypatch.setattr(
        refresh, "fetch_configuration", lambda *_, **__: VerifiedConfiguration(body, etag)
    )
    with pytest.raises(ConfigurationError, match="identity"):
        refresh.refresh(home=home)
    assert settings(paths) == before


def test_corrupt_cache_blocks_conditional_fetch(pilot, monkeypatch):
    home, _, paths, _, enroll = pilot
    enroll()
    cache = read(paths["cache"])
    cache["body"] += " "
    write(paths["cache"], cache)
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="integrity"):
        refresh.refresh(home=home)
    assert settings(paths) == before


def test_reenroll_cannot_silently_rebind_or_change_client_selection(pilot):
    _, descriptor, paths, _, enroll = pilot
    enroll()
    before = settings(paths)
    with pytest.raises(ConfigurationError):
        enroll(clients=["claude"])
    value = descriptor_value()
    value["tenant_id"] = OTHER
    write(descriptor, value)
    with pytest.raises(ConfigurationError):
        enroll()
    assert settings(paths) == before


@pytest.mark.parametrize("key", ["apiKeyHelper", "gateway"])
def test_unknown_existing_claude_route_is_never_silently_replaced(pilot, key):
    _, _, paths, _, enroll = pilot
    value = (
        {"apiKeyHelper": "/user/helper"}
        if key == "apiKeyHelper"
        else {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com"}}
    )
    write(paths["claude_cli"], value)
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="migration"):
        enroll(migrate_existing=True)
    assert settings(paths) == before


def test_remote_codex_catalog_requires_separate_review(pilot):
    _, _, paths, _, enroll = pilot
    write(paths["codex"], b'model_catalog_url = "https://other.example.com/catalog"\n')
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="remote Codex catalog"):
        enroll()
    assert settings(paths) == before


def test_matching_legacy_profiles_migrate_without_new_json_or_losing_import_policy(pilot):
    home, _, paths, _, enroll = pilot
    from claude_foundry import client, codex_client

    claude = {
        "tenant_id": descriptor_value()["tenant_id"],
        "auth": {"mode": "azure-cli", "tenant_id": descriptor_value()["tenant_id"]},
        "gateway_url": descriptor_value()["routes"]["claude"]["gateway_url"],
        "models": [
            {
                "name": "claude-sonnet-example",
                "anthropicFamilyTier": "sonnet",
                "isFamilyDefault": True,
            }
        ],
    }
    # Build exactly the legacy formats without invoking auth or a real client.
    legacy_state = home / ".config/claude-foundry"
    write(legacy_state / "client.json", claude)
    old_id = OTHER
    old_profile = paths["claude_profile"].parent / f"{old_id}.json"
    write(paths["claude_cli"], client.merge_cli({}, claude, legacy_state / "credential-helper"))
    write(legacy_state / "managed-installation.json", {"profile_id": old_id})
    write(
        old_profile,
        {
            **client.desktop_settings(claude, legacy_state / "credential-helper"),
            "claudeAiImport": {"enabled": False},
        },
    )
    old_codex_state = legacy_state / "codex"
    write(
        old_codex_state / "client.json",
        {
            "tenant_id": descriptor_value()["tenant_id"],
            "gateway_url": descriptor_value()["routes"]["codex"]["gateway_url"],
        },
    )
    old_provider = {
        "base_url": descriptor_value()["routes"]["codex"]["gateway_url"],
        "auth": {"command": str(old_codex_state / "credential-helper")},
    }
    values = {
        "model": "coding-example",
        "model_provider": codex_client.PROVIDER,
        "model_providers": {codex_client.PROVIDER: old_provider},
        "model_catalog_json": str(old_codex_state / "models.json"),
    }
    write(paths["codex"], tomlkit.dumps(values).encode())
    write(old_codex_state / "managed-installation.json", {"installed": copy.deepcopy(values)})
    enroll(migrate_existing=True)
    assert read(paths["claude_profile"])["claudeAiImport"] == {"enabled": False}
    assert old_profile.exists()  # Preserve the recoverable legacy profile.
    assert paths["helper"].read_bytes().endswith(b" enrolled-token\n")
    assert read(paths["binding"])["subject_id"] == SUBJECT


def test_migration_requires_matching_legacy_tenant(pilot):
    home, _, paths, _, enroll = pilot
    from claude_foundry import client

    legacy = home / ".config/claude-foundry"
    profile = {
        "tenant_id": OTHER,
        "gateway_url": descriptor_value()["routes"]["claude"]["gateway_url"],
        "models": [
            {
                "name": "claude-sonnet-example",
                "anthropicFamilyTier": "sonnet",
                "isFamilyDefault": True,
            }
        ],
    }
    write(paths["claude_cli"], client.merge_cli({}, profile, legacy / "credential-helper"))
    write(legacy / "client.json", profile)
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="Legacy profile identity"):
        enroll(migrate_existing=True)
    assert settings(paths) == before


def test_empty_grants_with_no_local_selection_remain_valid_settings(pilot, monkeypatch):
    home, _, paths, _, enroll = pilot
    enroll()
    claude = read(paths["claude_cli"])
    claude.pop("model")
    write(paths["claude_cli"], claude)
    codex = tomlkit.parse(paths["codex"].read_text())
    codex.pop("model")
    write(paths["codex"], tomlkit.dumps(codex).encode())
    value = configuration_value()
    value["state"] = "no_models"
    for entry in value["clients"].values():
        entry.update(default_model="", models=[])
    monkeypatch.setattr(refresh, "fetch_configuration", lambda *_, **__: verified(value))
    refresh.refresh(home=home)
    assert "model" not in read(paths["claude_cli"])
    assert "model" not in tomlkit.parse(paths["codex"].read_text())
    assert read(paths["catalog"]) == {"models": []}


@pytest.mark.parametrize("target", ["claude_cli", "codex", "claude_meta"])
def test_symlinked_local_target_rejected_before_any_settings_mutation(pilot, tmp_path, target):
    _, _, paths, _, enroll = pilot
    elsewhere = tmp_path / "user-file"
    write(elsewhere, {})
    paths[target].parent.mkdir(parents=True, exist_ok=True)
    paths[target].symlink_to(elsewhere)
    with pytest.raises(ConfigurationError, match="symlink"):
        enroll()
    assert elsewhere.read_bytes() == refresh._json({})
    assert not paths["binding"].exists()


def test_cli_enrolled_token_does_not_trigger_install_or_signin(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.sys, "argv", ["claude-foundry", "enrolled-token"])
    monkeypatch.setattr(refresh, "inference_token", lambda: "test-credential")
    assert cli.main() == 0
    assert capsys.readouterr().out == "test-credential\n"


def test_cli_custom_codex_home_rejected_before_enrollment(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.sys, "argv", ["claude-foundry", "refresh"])
    monkeypatch.setattr(cli.client, "check_managed_preferences", lambda: None)
    monkeypatch.setattr(cli.codex_client, "check_managed_preferences", lambda: None)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
    assert cli.main() == 1
    assert "custom CODEX_HOME" in capsys.readouterr().err
