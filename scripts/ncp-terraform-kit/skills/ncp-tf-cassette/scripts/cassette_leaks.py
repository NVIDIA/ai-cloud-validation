# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""What a cassette may not hold, and the placeholders that stand for it.

A cassette is a committed file. An API can echo a value as it is, URL-encoded,
JSON-escaped or base64-encoded, and can answer with a credential that no
variable holds. So each replaced value is replaced in each of these forms, and
a recording or a check refuses what is left.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import urllib.parse
from collections.abc import Callable, Sequence
from typing import Any

import attribute_names

#: A private key, or the key field of a credential file. A response can return
#: a key that no variable holds, such as the key that a create made.
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\\?"private_key\\?"\s*:')
#: A run of base64 long enough to hide a key. Each one is decoded and read again.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{40,}")
#: A placeholder: the only value that a recorded secret field may hold.
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: A secret field that the API returns masked, as a row of asterisks, holds no
#: secret.
MASK = re.compile(r"\*+")
#: The prefix of each common credential that its issuer gives a fixed form.
#: No variable holds a token that an API made, so its shape is what shows it.
TOKEN_SHAPES = {
    "a Google OAuth access token": re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    "a Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    "an AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "a GitLab token": re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}"),
    "a GitHub token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}"),
    "a JWT": re.compile(r"\beyJ[0-9A-Za-z_-]{8,}\.eyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]*"),
}
#: The JSON fields, in snake case, that hold a credential whatever the
#: provider's schema export marks.
SECRET_NAMES = frozenset({"client_secret", "password", "access_token", "refresh_token", "secret", "private_key"})
#: The query parameters, in snake case, that carry a credential.
CREDENTIAL_PARAMETER = re.compile(
    r"(?:api_?)?key|(?:access|auth|id|refresh)_?token|token|sig|signature|password|passwd|(?:client_?)?secret"
)


def credential(value: str) -> list[tuple[str, str]]:
    """The credential, and its token alone when a scheme such as `Bearer` opens
    it: an API can echo either."""
    _, _, token = value.partition(" ")
    return [(value, "{credential}")] + ([(token.strip(), "{credential}")] if token.strip() else [])


def forms(value: str) -> list[str]:
    """The value as it is, and as an API can echo it encoded."""
    data = value.encode()
    encoded = [base64.b64encode(data).decode(), base64.urlsafe_b64encode(data).decode()]
    return list(
        dict.fromkeys(
            [
                value,
                urllib.parse.quote(value, safe=""),
                urllib.parse.quote(value),
                urllib.parse.quote_plus(value),
                json.dumps(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
                *encoded,
                *(form.rstrip("=") for form in encoded),
            ]
        )
    )


def expand(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each form of each value, with what stands in its place; longest first.
    A form that two values share takes the stand-in of the first."""
    found: dict[str, str] = {}
    for value, stand_in in pairs:
        for form in forms(value):
            found.setdefault(form, stand_in)
    return sorted(found.items(), key=lambda pair: -len(pair[0]))


def scrub(text: str, pairs: Sequence[tuple[str, str]]) -> str:
    for value, stand_in in expand(pairs):
        text = text.replace(value, stand_in)
    return text


def _decoded_runs(text: str) -> list[str]:
    found = []
    for run in BASE64_RUN.findall(text):
        usable = run[: len(run) - len(run) % 4].replace("-", "+").replace("_", "/")
        try:
            found.append(base64.b64decode(usable).decode("utf-8", "ignore"))
        except ValueError:
            continue
    return found


def holds_private_key(text: str) -> bool:
    """Whether the text holds a private key, as it is or base64-encoded."""
    return any(PRIVATE_KEY.search(part) for part in [text, *_decoded_runs(text)])


def raw_values(text: str, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """What stands for each value that the text holds in one of its forms, or
    inside a base64 run, such as a basic credential."""
    decoded = _decoded_runs(text)
    return list(
        dict.fromkeys(
            stand_in
            for value, stand_in in pairs
            if any(form in text for form in forms(value)) or any(value in part for part in decoded)
        )
    )


def token_shapes(text: str) -> list[str]:
    return [label for label, shape in TOKEN_SHAPES.items() if shape.search(text)]


def _held(value: Any) -> bool:
    return value not in ("", None) and not (
        isinstance(value, str) and (PLACEHOLDER.fullmatch(value) or MASK.fullmatch(value))
    )


def _fields(body: str, wanted: Callable[[str, Any], bool]) -> list[str]:
    """The path of each field of a JSON body that `wanted` takes."""
    try:
        document = json.loads(body)
    except ValueError:
        return []
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, path)
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            name = attribute_names.terraform_name(str(key)) if path else attribute_names.root_name(str(key))
            if wanted(name, value):
                found.append(here)
            walk(value, here)

    walk(document, "")
    return found


def secret_fields(body: str, marked: set[str]) -> list[str]:
    """Each field of a JSON body that the export marks sensitive and that holds
    a value other than a placeholder."""
    return _fields(body, lambda key, value: key in marked and _held(value))


def named_secrets(body: str) -> list[str]:
    """Each field named like a secret that holds text other than a mask, or
    than text around a placeholder, such as `Bearer {credential}`."""
    return _fields(
        body,
        lambda key, value: (
            key in SECRET_NAMES and isinstance(value, str) and _held(value) and not PLACEHOLDER.search(value)
        ),
    )


def secrets_held(texts: Sequence[str], marked: set[str], pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Each thing that the texts hold and a cassette may not, as a phrase."""
    found = []
    fields = [field for text in texts for field in secret_fields(text, marked)]
    if fields:
        found.append(f"the fields {', '.join(fields)}, which the provider's schema export marks sensitive")
    found += [f"the secret field {field}" for text in texts for field in named_secrets(text)]
    joined = "\n".join(texts)
    if holds_private_key(joined):
        found.append("a private key")
    found += token_shapes(joined)
    found += [f"the raw value that {stand_in} stands for" for stand_in in raw_values(joined, pairs)]
    return found


def not_recorded(document: Any, forms: dict[str, str], values: dict[str, str]) -> list[str]:
    """Each supplied name whose value cannot be the one that the recording of
    the cassette replaced.

    A check can run in another shell than the recording. The cassette records
    under `supplied` the form of each value that held the run id or an account,
    and no form for another value. So a value whose form in `forms` differs
    from the recorded one is another value, and the cassette cannot leak it.
    """
    recorded = (document.get("supplied") if isinstance(document, dict) else None) or {}
    return [name for name, value in values.items() if value and forms.get(name) != recorded.get(name)]


def url_problem(url: str, pairs: Sequence[tuple[str, str]], token: str | None) -> str | None:
    """Why the URL may not be sent, or None. A cassette holds the URL, so it may
    carry no credential that the recording does not replace."""
    parts = urllib.parse.urlsplit(url)
    if not (parts.scheme == "https" or (parts.scheme == "http" and _loopback(parts.hostname or ""))):
        return (
            f"the URL's scheme is {parts.scheme or 'missing'}: a recording sends https to the API, or "
            "http to a loopback host where a local stub answers"
        )
    if "@" in parts.netloc:
        return (
            "the URL holds user information, and a cassette holds the URL. Send the credential with "
            "--auth-header and --auth-env"
        )
    replaced = {value for value, _ in pairs}
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if (
            name != token
            and value
            and value not in replaced
            and CREDENTIAL_PARAMETER.fullmatch(attribute_names.terraform_name(name))
        ):
            return (
                f"the URL's query parameter {name} holds a value that no variable names, and a cassette "
                "holds the URL. Put the value in an environment variable and name it with --redact "
                "<VARIABLE>: the cassette then holds {<VARIABLE>} in its place"
            )
    return None


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
