"""Account-bound credentials for an explicitly enrolled user.

Azure CLI owns schema-3 credentials; encrypted MSAL persistence supports schema 2.
This adapter never exports a credential cache or falls back between providers.
JWT payload inspection is only a consistency guard. The configuration API and
governed inference gateway verify signatures and authorize the actual request.
"""

import base64
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .azure import find_az
from .config import TOKEN_RESOURCE
from .configuration_client import (
    AZURE_CLI_CLIENT_ID,
    EnrollmentDescriptor,
    SignInRequired,
    _pairs,
    _uuid,
)
from .local_update import _read

SIGN_IN_REQUIRED = (
    "Organization sign-in is required or the enrolled account changed; "
    "run foreground refresh --login. Background helpers cannot sign in."
)


class EnrolledAzureCli:
    """Reuse CLI-owned sign-in, but never follow its default tenant/user blindly.

    No token files are created by this adapter. Azure CLI owns its credential
    storage and renewal. The dedicated configuration audience remains separate
    from Cognitive Services and from the portal administration application.
    """

    def __init__(self, descriptor, directory, *, subject_id=None):
        if (
            not isinstance(descriptor, EnrollmentDescriptor)
            or descriptor.schema_version != 3
            or descriptor.public_client_id != AZURE_CLI_CLIENT_ID
        ):
            raise SignInRequired("A reviewed Azure CLI enrollment descriptor is required.")
        self.descriptor = descriptor
        self.subject_id = _uuid(subject_id) if subject_id is not None else None

    def login(self):
        return self._token(configuration=True, interactive=True)

    def configuration_token(self):
        if self.subject_id is None:
            raise SignInRequired(SIGN_IN_REQUIRED)
        return self._token(configuration=True)

    def inference_token(self, *, interactive=False):
        if self.subject_id is None:
            raise SignInRequired(SIGN_IN_REQUIRED)
        return self._token(configuration=False, interactive=interactive)

    def _command(self, *args, interactive=False):
        result = subprocess.run(
            [find_az(), *args, "--only-show-errors", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=300 if interactive else 45,
            env={**os.environ, "AZURE_CORE_LOGIN_EXPERIENCE_V2": "off"},
        )
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError
        return json.loads(result.stdout, object_pairs_hook=_pairs) if result.stdout else None

    def _token(self, *, configuration, interactive=False):
        target = (
            ("--scope", self.descriptor.scope) if configuration else ("--resource", TOKEN_RESOURCE)
        )
        try:
            try:
                return self._acquire(target, configuration)
            except Exception:
                if not interactive:
                    raise
            # Explicit foreground recovery only. Never change the default
            # subscription, log out other accounts, or start device-code login.
            self._command(
                "login",
                "--tenant",
                self.descriptor.tenant_id,
                "--allow-no-subscriptions",
                "--scope",
                self.descriptor.scope if configuration else TOKEN_RESOURCE + "/.default",
                interactive=True,
            )
            return self._acquire(target, configuration)
        except Exception:
            # CLI stderr can include identities and diagnostic response bodies.
            raise SignInRequired(SIGN_IN_REQUIRED) from None

    def _acquire(self, target, configuration):
        result = self._command(
            "account", "get-access-token", "--tenant", self.descriptor.tenant_id, *target
        )
        token = result["accessToken"]
        claims = _claims(token)
        subject = _uuid(claims.get("oid"))
        if (
            claims.get("tid") != self.descriptor.tenant_id
            or claims.get("aud")
            != (self.descriptor.configuration_resource_id if configuration else TOKEN_RESOURCE)
            or (self.subject_id is not None and subject != self.subject_id)
            or claims.get("azp", claims.get("appid")) != AZURE_CLI_CLIENT_ID
            or claims.get("idtyp") == "app"
            or not isinstance(claims.get("scp"), str)
            or ("configuration.read" if configuration else "user_impersonation")
            not in claims["scp"].split()
            or type(claims.get("exp")) not in {int, float}
            or not math.isfinite(claims["exp"])
            or claims["exp"] <= time.time()
        ):
            raise ValueError
        return token


def _cache_directory(directory):
    directory = Path(directory)
    if not directory.is_absolute() or ".." in directory.parts:
        raise SignInRequired("An absolute private credential directory is required.")
    # Do not follow a substituted state directory (including parent symlinks).
    for path in reversed([directory, *directory.parents]):
        if path.is_symlink():
            raise SignInRequired("Credential directories cannot be symlinks.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SignInRequired("Credential directory must be owned by you with mode 0700.")
    return directory


def _claims(token):
    if (
        not isinstance(token, str)
        or len(token) > 16384
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
    ):
        raise SignInRequired("The credential has an unsupported format.")
    try:
        encoded = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)),
            object_pairs_hook=_pairs,
        )
        if not isinstance(claims, dict):
            raise ValueError
        return claims
    except (ValueError, RecursionError):
        raise SignInRequired("The credential has an unsupported format.") from None


def _application(descriptor, directory):
    """Create encrypted persistence before any authentication request."""
    import msal
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence

    location = directory / "token-cache"
    # MSAL extensions manages its own cross-process lock. Reject substituted
    # marker/cache/lock files before passing paths to that library.
    _read(location)
    _read(location.with_name(location.name + ".lockfile"))
    persistence = build_encrypted_persistence(str(location))
    if not persistence.is_encrypted:
        raise RuntimeError("Encrypted credential storage is required.")
    return msal.PublicClientApplication(
        descriptor.public_client_id,
        authority=f"https://login.microsoftonline.com/{descriptor.tenant_id}",
        token_cache=PersistedTokenCache(persistence),
        timeout=10,
        allow_broker=False,
        enable_pii_log=False,
    )


@contextmanager
def _keychain_interaction(interactive):
    """Suppress macOS key-store dialogs in this short-lived CLI/helper process."""
    if sys.platform != "darwin" or interactive:
        yield
        return
    import ctypes

    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    get_allowed = security.SecKeychainGetUserInteractionAllowed
    get_allowed.argtypes, get_allowed.restype = [ctypes.POINTER(ctypes.c_ubyte)], ctypes.c_int32
    set_allowed = security.SecKeychainSetUserInteractionAllowed
    set_allowed.argtypes, set_allowed.restype = [ctypes.c_ubyte], ctypes.c_int32
    previous = ctypes.c_ubyte()
    if get_allowed(ctypes.byref(previous)) != 0 or set_allowed(0) != 0:
        raise RuntimeError("Cannot disable credential dialogs.")
    try:
        yield
    finally:
        if set_allowed(previous.value) != 0:
            raise RuntimeError("Cannot restore credential dialog policy.")


class EnrolledMsal:
    """Explicit browser login; silent refresh for one immutable tenant/user."""

    def __init__(self, descriptor, directory, *, subject_id=None):
        if not isinstance(descriptor, EnrollmentDescriptor):
            raise SignInRequired("A reviewed enrollment descriptor is required.")
        self.descriptor = descriptor
        root = _cache_directory(directory)
        self.directory = _cache_directory(root / descriptor.tenant_id / descriptor.public_client_id)
        self.subject_id = _uuid(subject_id) if subject_id is not None else None

    def login(self):
        """Foreground only: select an account explicitly, never by cache ordering."""
        return self._token(
            self.descriptor.scope,
            self.descriptor.configuration_resource_id,
            "configuration.read",
            interactive=True,
        )

    def configuration_token(self):
        return self._token(
            self.descriptor.scope,
            self.descriptor.configuration_resource_id,
            "configuration.read",
        )

    def inference_token(self, *, interactive=False):
        if self.subject_id is None:
            raise SignInRequired("Complete organization enrollment before requesting inference.")
        return self._token(
            TOKEN_RESOURCE + "/.default",
            TOKEN_RESOURCE,
            "user_impersonation",
            interactive=interactive,
        )

    def _token(self, scope, audience, permission, *, interactive=False):
        try:
            with _keychain_interaction(interactive):
                return self._acquire(scope, audience, permission, interactive=interactive)
        except Exception:
            # Persistence/network/MSAL exceptions may include account details,
            # diagnostic URLs and response bodies. Never expose or log them.
            # An unavailable key store fails closed, including during login.
            raise SignInRequired(SIGN_IN_REQUIRED) from None

    def _acquire(self, scope, audience, permission, *, interactive):
        app = _application(self.descriptor, self.directory)
        accounts = (
            [
                account
                for account in app.get_accounts()
                if account.get("realm") == self.descriptor.tenant_id
                and account.get("local_account_id") == self.subject_id
            ]
            if self.subject_id is not None
            else []
        )
        if len(accounts) > 1:
            raise ValueError
        account = accounts[0] if accounts else None
        result = app.acquire_token_silent_with_error([scope], account=account) if account else None
        if interactive and (not result or "access_token" not in result):
            # MSAL implements the browser/loopback PKCE flow. Deliberately
            # do not support a background/device-code/password fallback.
            result = app.acquire_token_interactive(
                scopes=[scope], prompt="select_account", timeout=300
            )
        if not isinstance(result, dict):
            raise ValueError
        token = result.get("access_token")
        claims = _claims(token)
        subject = _uuid(claims.get("oid"))
        if (
            claims.get("tid") != self.descriptor.tenant_id
            or claims.get("aud") != audience
            or (self.subject_id is not None and subject != self.subject_id)
            or claims.get("azp", claims.get("appid")) != self.descriptor.public_client_id
            or claims.get("idtyp") == "app"
            or not isinstance(claims.get("scp"), str)
            or permission not in claims["scp"].split()
            or type(claims.get("exp")) not in {int, float}
            or not math.isfinite(claims["exp"])
            or not claims["exp"] > time.time()
        ):
            raise ValueError
        matches = [
            account
            for account in app.get_accounts()
            if account.get("realm") == self.descriptor.tenant_id
            and account.get("local_account_id") == subject
        ]
        if len(matches) != 1:
            raise ValueError
        return token
