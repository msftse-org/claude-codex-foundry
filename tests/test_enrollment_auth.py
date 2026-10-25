import base64
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_configuration_client import PUBLIC_CLIENT, RESOURCE, SUBJECT, TENANT, descriptor_value

from claude_foundry import enrollment_auth as auth
from claude_foundry.config import TOKEN_RESOURCE
from claude_foundry.configuration_client import (
    ConfigurationError,
    EnrollmentDescriptor,
    SignInRequired,
)

OTHER = "00000000-0000-0000-0000-000000000099"


def descriptor():
    return EnrollmentDescriptor.parse(descriptor_value())


def claims(**overrides):
    return {
        "tid": TENANT,
        "oid": SUBJECT,
        "aud": RESOURCE,
        "azp": PUBLIC_CLIENT,
        "scp": "configuration.read",
        "exp": 2000000000,
        **overrides,
    }


def token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    # Not a signature-valid token: API/gateway validation remains authoritative.
    return f"header.{encoded}.signature"


def account(subject=SUBJECT, tenant=TENANT):
    return {"realm": tenant, "local_account_id": subject, "home_account_id": "opaque-id"}


@pytest.fixture
def application(monkeypatch):
    app = SimpleNamespace(
        accounts=[account()],
        result={"access_token": token(claims())},
        calls=[],
        error=None,
    )

    def accounts():
        return app.accounts

    def silent(scopes, *, account):
        app.calls.append(("silent", scopes, account))
        if app.error:
            raise app.error
        return app.result

    def interactive(**kwargs):
        app.calls.append(("interactive", kwargs))
        if app.error:
            raise app.error
        return app.result

    @contextmanager
    def keychain(interactive):
        app.calls.append(("keychain", interactive))
        yield

    monkeypatch.setattr(auth, "_application", lambda *_: app)
    monkeypatch.setattr(auth, "_keychain_interaction", keychain)
    monkeypatch.setattr(auth.time, "time", lambda: 1900000000)
    app.get_accounts = accounts
    app.acquire_token_silent_with_error = silent
    app.acquire_token_interactive = interactive
    return app


def test_separate_audiences_encrypted_cache_and_no_normal_azure_cache(
    application, tmp_path, monkeypatch
):
    monkeypatch.setenv("AZURE_CONFIG_DIR", "/unrelated/normal-azure-cache")
    root = tmp_path / "credentials"
    provider = auth.EnrolledMsal(descriptor(), root, subject_id=SUBJECT)
    assert provider.configuration_token() == token(claims())
    assert application.calls == [
        ("keychain", False),
        ("silent", [f"api://{RESOURCE}/configuration.read"], account()),
    ]
    payload = claims(aud=TOKEN_RESOURCE, scp="user_impersonation")
    application.result = {"access_token": token(payload)}
    assert provider.inference_token() == token(payload)
    assert application.calls[-1] == ("silent", [TOKEN_RESOURCE + "/.default"], account())
    assert provider.directory == root / TENANT / PUBLIC_CLIENT
    assert root.stat().st_mode & 0o777 == provider.directory.stat().st_mode & 0o777 == 0o700
    assert os.environ["AZURE_CONFIG_DIR"] == "/unrelated/normal-azure-cache"
    assert not list(provider.directory.iterdir())  # No token written by our wrapper.


def test_initial_login_requires_explicit_account_selection(application, tmp_path):
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials")
    # Even with cached accounts, unbound/background reads never select one.
    with pytest.raises(SignInRequired):
        provider.configuration_token()
    assert application.calls == [("keychain", False)]
    with pytest.raises(SignInRequired, match="Complete organization enrollment"):
        provider.inference_token()
    application.calls.clear()
    assert provider.login() == token(claims())
    assert application.calls == [
        ("keychain", True),
        (
            "interactive",
            {"scopes": [descriptor().scope], "prompt": "select_account", "timeout": 300},
        ),
    ]
    assert provider.subject_id is None  # Only an authenticated API response can bind it.


def test_only_pinned_resource_tenant_account_is_selected(application, tmp_path):
    application.accounts = [account(OTHER), account(SUBJECT, OTHER), account()]
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    assert provider.configuration_token()
    assert application.calls[-1] == ("silent", [descriptor().scope], account())
    application.accounts = [account(OTHER)]
    application.calls.clear()
    with pytest.raises(SignInRequired):
        provider.configuration_token()
    assert application.calls == [("keychain", False)]


@pytest.mark.parametrize("accounts", [[], [account(), account()]])
def test_missing_or_ambiguous_account_never_prompts(application, tmp_path, accounts):
    application.accounts = accounts
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    with pytest.raises(SignInRequired):
        provider.configuration_token()
    assert application.calls == [("keychain", False)]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tid", OTHER),
        ("oid", OTHER),
        ("oid", None),
        ("oid", "email@example.com"),
        ("aud", "https://management.azure.com"),
        ("aud", [RESOURCE]),
        ("azp", OTHER),
        ("azp", "04b07795-8ddb-461a-bbee-02f9e1bf7b46"),
        ("idtyp", "app"),
        ("scp", ""),
        ("scp", ["configuration.read"]),
        ("scp", "configuration.reader"),
        ("exp", 1),
        ("exp", True),
        ("exp", "2000000000"),
        ("exp", float("nan")),
        ("exp", float("inf")),
    ],
)
def test_identity_audience_client_scope_and_expiry_guards(application, tmp_path, key, value):
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    application.result = {"access_token": token(claims(**{key: value}))}
    with pytest.raises(SignInRequired):
        provider.configuration_token()
    assert all(call[0] != "interactive" for call in application.calls)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        [],
        {"access_token": None},
        {"access_token": "secret"},
        {"error": "interaction_required", "error_description": "secret"},
    ],
)
def test_safe_noninteractive_error_without_diagnostic_leaks(application, tmp_path, result, capsys):
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    application.result = result
    with pytest.raises(SignInRequired) as caught:
        provider.configuration_token()
    assert "secret" not in str(caught.value)
    assert all(call[0] != "interactive" for call in application.calls)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_inference_wrong_identity_and_silent_failure_never_emit_token(
    application, tmp_path, capsys
):
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    application.result = {
        "access_token": token(claims(oid=OTHER, aud=TOKEN_RESOURCE, scp="user_impersonation"))
    }
    with pytest.raises(SignInRequired):
        provider.inference_token()
    application.error = OSError("secret-keychain-account")
    with pytest.raises(SignInRequired) as caught:
        provider.inference_token()
    assert "secret" not in str(caught.value)
    assert capsys.readouterr().out == ""


def test_factory_requires_encryption_before_any_msal_or_network_call(tmp_path, monkeypatch):
    import msal
    import msal_extensions

    directory = auth._cache_directory(tmp_path / "credentials")
    calls = []

    def persistence(location):
        calls.append(location)
        return SimpleNamespace(is_encrypted=False)

    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", persistence)
    monkeypatch.setattr(
        msal, "PublicClientApplication", lambda *_, **__: pytest.fail("MSAL created")
    )
    with pytest.raises(RuntimeError, match="Encrypted"):
        auth._application(descriptor(), directory)
    assert calls == [str(directory / "token-cache")]


def test_factory_uses_tenant_specific_application_and_encrypted_persisted_cache(
    tmp_path, monkeypatch
):
    import msal
    import msal_extensions

    directory = auth._cache_directory(tmp_path / "credentials")
    persistence, cache = SimpleNamespace(is_encrypted=True), object()
    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", lambda _: persistence)

    def persisted(value):
        assert value is persistence
        return cache

    calls = []
    monkeypatch.setattr(msal_extensions, "PersistedTokenCache", persisted)
    monkeypatch.setattr(msal, "PublicClientApplication", lambda *a, **k: calls.append((a, k)))
    auth._application(descriptor(), directory)
    assert calls == [
        (
            (PUBLIC_CLIENT,),
            {
                "authority": f"https://login.microsoftonline.com/{TENANT}",
                "token_cache": cache,
                "timeout": 10,
                "allow_broker": False,
                "enable_pii_log": False,
            },
        )
    ]


@pytest.mark.parametrize("interactive", [False, True])
def test_unavailable_key_store_never_falls_back(application, tmp_path, monkeypatch, interactive):
    def unavailable(*_):
        raise OSError("secret-keychain-details")

    monkeypatch.setattr(auth, "_application", unavailable)
    provider = auth.EnrolledMsal(descriptor(), tmp_path / "credentials", subject_id=SUBJECT)
    with pytest.raises(SignInRequired) as caught:
        provider.login() if interactive else provider.configuration_token()
    assert "secret" not in str(caught.value)
    assert application.calls == [("keychain", interactive)]


@pytest.mark.parametrize("filename", ["token-cache", "token-cache.lockfile"])
def test_factory_refuses_substituted_cache_marker_and_lock(tmp_path, filename):
    directory = auth._cache_directory(tmp_path / "credentials")
    target = tmp_path / "target"
    target.write_text("not a token")
    (directory / filename).symlink_to(target)
    with pytest.raises(ConfigurationError, match="symlinks"):
        auth._application(descriptor(), directory)


def test_credential_directory_permissions_not_silently_repaired(tmp_path):
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o755)
    with pytest.raises(SignInRequired, match="0700"):
        auth.EnrolledMsal(descriptor(), directory, subject_id=SUBJECT)
    assert directory.stat().st_mode & 0o777 == 0o755


def test_symlinked_cache_or_parent_is_not_followed(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    for directory in [link, link / "credentials"]:
        with pytest.raises(SignInRequired, match="symlinks"):
            auth.EnrolledMsal(descriptor(), directory, subject_id=SUBJECT)
    assert not list(target.iterdir())


@pytest.mark.parametrize("directory", ["relative/credentials", "/absolute/../credentials"])
def test_ambiguous_cache_path_rejected(directory):
    with pytest.raises(SignInRequired, match="absolute"):
        auth.EnrolledMsal(descriptor(), directory, subject_id=SUBJECT)


def test_macos_background_disables_and_restores_key_store_interaction(monkeypatch):
    import ctypes

    calls = []

    def get_allowed(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte)).contents.value = 1
        return 0

    def set_allowed(value):
        calls.append(value)
        return 0

    library = SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=get_allowed,
        SecKeychainSetUserInteractionAllowed=set_allowed,
    )
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda _: library)
    with pytest.raises(ValueError), auth._keychain_interaction(False):
        assert calls == [0]
        raise ValueError
    assert calls == [0, 1]
    with auth._keychain_interaction(True):
        pass
    assert calls == [0, 1]


def test_macos_cannot_disable_dialogs_fails_before_cache_access(monkeypatch):
    import ctypes

    def get_allowed(_):
        return -1

    library = SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=get_allowed,
        SecKeychainSetUserInteractionAllowed=lambda _: pytest.fail("write"),
    )
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda _: library)
    with pytest.raises(RuntimeError, match="disable"):
        with auth._keychain_interaction(False):
            pytest.fail("cache accessed")
