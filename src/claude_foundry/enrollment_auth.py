"""Isolated Azure CLI credentials for an explicitly enrolled account.

JWT payload inspection here is only an account/audience consistency guard.
The configuration API and governed inference gateway validate the signatures
and authorize every request. Never use these inspected claims as an access grant.
Azure CLI's macOS/Linux cache is plaintext; this pilot does not claim otherwise.
"""

import base64
import json
import math
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from .azure import find_az
from .config import TOKEN_RESOURCE
from .configuration_client import (
    ConfigurationError,
    EnrollmentDescriptor,
    SignInRequired,
    _pairs,
    _uuid,
)

AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


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


class EnrolledAzureCli:
    """No implicit login, shared cache, account selection or token persistence."""

    def __init__(self, descriptor, directory, *, subject_id=None):
        if not isinstance(descriptor, EnrollmentDescriptor):
            raise SignInRequired("A reviewed enrollment descriptor is required.")
        self.descriptor = descriptor
        self.directory = _cache_directory(directory)
        self.subject_id = _uuid(subject_id) if subject_id is not None else None

    def _environment(self):
        return dict(
            os.environ,
            AZURE_CONFIG_DIR=str(self.directory),
            AZURE_CORE_LOGIN_EXPERIENCE_V2="off",
            AZURE_CORE_COLLECT_TELEMETRY="no",
        )

    def login(self):
        """Explicit foreground enrollment/reauthentication only; never called by token()."""
        try:
            # Leave the browser/device-code instructions visible in this explicit
            # interactive action. No token is printed or returned by `az login`.
            result = subprocess.run(
                [
                    find_az(),
                    "login",
                    "--tenant",
                    self.descriptor.tenant_id,
                    "--allow-no-subscriptions",
                    "--scope",
                    self.descriptor.scope,
                    "--only-show-errors",
                    "--output",
                    "none",
                ],
                env=self._environment(),
                timeout=360,
            )
            if result.returncode:
                raise SignInRequired("Organization sign-in did not complete.")
        except (OSError, subprocess.TimeoutExpired):
            raise SignInRequired("Organization sign-in did not complete.") from None
        return self.configuration_token()

    def configuration_token(self):
        return self._token(
            self.descriptor.scope,
            self.descriptor.configuration_resource_id,
            "configuration.read",
        )

    def inference_token(self):
        if self.subject_id is None:
            raise SignInRequired("Complete organization enrollment before requesting inference.")
        return self._token(TOKEN_RESOURCE + "/.default", TOKEN_RESOURCE, "user_impersonation")

    def _token(self, scope, audience, permission):
        try:
            result = subprocess.run(
                [
                    find_az(),
                    "account",
                    "get-access-token",
                    "--tenant",
                    self.descriptor.tenant_id,
                    "--scope",
                    scope,
                    "--only-show-errors",
                    "--output",
                    "json",
                ],
                env=self._environment(),
                text=True,
                capture_output=True,
                timeout=30,
            )
            if result.returncode or len(result.stdout) > 32768:
                raise ValueError
            response = json.loads(result.stdout, object_pairs_hook=_pairs)
            token = response["accessToken"]
            claims = _claims(token)
            subject = _uuid(claims.get("oid"))
            if (
                claims.get("tid") != self.descriptor.tenant_id
                or claims.get("aud") != audience
                or (self.subject_id is not None and subject != self.subject_id)
                or claims.get("azp", claims.get("appid")) != AZURE_CLI_CLIENT_ID
                or claims.get("idtyp") == "app"
                or not isinstance(claims.get("scp"), str)
                or permission not in claims["scp"].split()
                or type(claims.get("exp")) not in {int, float}
                or not math.isfinite(claims["exp"])
                or not claims["exp"] > time.time()
            ):
                raise ValueError
            return token
        except (
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            OSError,
            subprocess.TimeoutExpired,
            ConfigurationError,
        ):
            # CLI stderr can contain tenant/account details or diagnostic URLs.
            # Never echo it, stdout, the token or the underlying exception.
            raise SignInRequired(
                "Organization sign-in is required or the enrolled account changed; "
                "background helpers cannot sign in."
            ) from None
