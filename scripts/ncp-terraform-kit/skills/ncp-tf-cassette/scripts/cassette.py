#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
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
"""Record one exchange with the NCP's API into a cassette, and check cassettes.

    uv run scripts/cassette.py record --cases <cases> --out <cassettes> \
        --provider-export <provider-schema-export.json> \
        --name <the case's cassette name> --method POST --url <url> [--body <file>] \
        [--auth-header Authorization --auth-env <VARIABLE>] \
        [--header <NAME>=<VARIABLE> ...] [--redact <VARIABLE>[=<ACCOUNT>] ...] [--after <verb>] \
        [--after-request <cassette>:<index>] [--token <query parameter>]
    uv run scripts/cassette.py check --cases <cases> --cassettes <cassettes> \
        [--auth-env <VARIABLE>] [--redact <VARIABLE>[=<ACCOUNT>] ...]

`record` sends one request, the one the agent built from the case, directly to
the API, and appends the request and the response to the named cassette. It
never records the provider's traffic, and nothing in a test run calls it. The
authentication header is sent and never written. A cassette is a committed
fixture, so the run id, each value that the run supplies to the case and each
`--redact` variable become placeholders, and only the headers that the case
asserts stay. An exchange that failed, or that holds a secret, is not written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _skill_scripts(skill: str) -> Path:
    """The scripts of another kit skill, installed beside this one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / skill / "scripts"
        if sibling.is_dir():
            return sibling
    raise SystemExit(f"cassette: no {skill} skill beside this one; install the kit's skills together")


# The leak check names a body's field by the provider's conversion, because the
# provider's schema export marks a sensitive field by the name it declares.
sys.path.append(str(_skill_scripts("ncp-tf-provider")))

from cassette_leaks import PLACEHOLDER, credential, not_recorded, scrub, secrets_held, url_problem  # noqa: E402

API_VERSION = "terraform.ncp/v1"
KIND = "cassette"
#: The verbs a read can follow. A read is served by the phase of the lifecycle
#: it was recorded in, so replay can answer a read after an update with what
#: the API returned then.
PHASES = ("create", "update", "delete")
#: How long one request may take. A create that the API answers slowly still
#: returns an operation long before this; a hung connection does not.
TIMEOUT_SECONDS = 120
#: The headers a cassette may never hold, whatever a case says.
CREDENTIALS = re.compile(r"^(authorization|proxy-authorization|cookie|x-api-key)$", re.IGNORECASE)
#: The shortest value that a recording replaces with a placeholder. A shorter
#: value, such as `true` or a small number, also occurs in text that is not
#: that value, and replacing it there breaks the recorded JSON.
MIN_REPLACED_LENGTH = 6
#: The run id that the provider's entry point makes. A name's room was measured
#: for it, so a longer id makes a name that the API refuses.
RUN_ID_FORM = re.compile(r"tfkit[0-9a-z]{8}")
#: A cassette name is a file name in the output directory: no separator, no dot.
CASSETTE_NAME_FORM = re.compile(r"[a-z0-9_-]+")
#: An environment variable: the form of a placeholder that replay fills from
#: the environment, where the provider reads its own settings.
ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
#: What a recording holds for a de-duplication token. The provider makes a new
#: token for each request, so replay accepts any value in that parameter.
DEDUP_TOKEN = "{dedup_token}"


class CassetteError(ValueError):
    """The request cannot be recorded, or a cassette breaks the format."""


#: Placeholders that the run does not supply: the run id is its own, and the
#: renderer fills `{case}` in a dependency's reference with the resource's kind.
NOT_SUPPLIED = frozenset({"run_id", "case"})


def _supplied(case: dict[str, Any]) -> list[str]:
    """The name of each value that a run supplies to the case: each placeholder
    of its fixture and of its dependencies, and each dependency's variable."""
    text = json.dumps([case.get("fixture"), case.get("dependencies")])
    names = set(PLACEHOLDER.findall(text)) | {
        str(d["variable"]) for d in case.get("dependencies") or [] if isinstance(d, dict) and d.get("variable")
    }
    return sorted(names - NOT_SUPPLIED)


def _named(case: dict[str, Any]) -> list[str]:
    """Each dependency variable whose value is a name: the test builds the form
    its case's API takes around it, from the dependency's reference. An
    `attribute` dependency takes a value the API assigned, as it is."""
    return sorted(
        str(d["variable"])
        for d in case.get("dependencies") or []
        if isinstance(d, dict) and d.get("variable") and d.get("reference") and not d.get("attribute")
    )


def expected(cases: Path) -> dict[str, dict[str, Any]]:
    """Each cassette name the cases hold, with its operation, its asserted
    headers, its Terraform type and the values that a run supplies to it."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(cases.rglob("*.yaml")):
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not (isinstance(case, dict) and case.get("contract") == "resource"):
            continue
        address = str((case.get("resource") or {}).get("terraform_address") or "")
        for request in case.get("expected_requests") or []:
            if not isinstance(request, dict) or not request.get("cassette"):
                continue
            headers = [
                str(h.get("name"))
                for h in request.get("headers") or []
                if isinstance(h, dict)
                and h.get("name")
                and not str(h.get("source") or "").startswith("auth")
                and not CREDENTIALS.match(str(h.get("name")))
            ]
            # A header that a security scheme gives carries a credential.
            secured = [
                str(h.get("name")).lower()
                for h in request.get("headers") or []
                if isinstance(h, dict) and str(h.get("source") or "").startswith("security:")
            ]
            found[str(request["cassette"])] = {
                "operation": str(request.get("ref") or ""),
                "verb": str(request.get("verb") or ""),
                "headers": headers,
                "type": address.partition(".")[0],
                "supplied": _supplied(case),
                "named": _named(case),
                "secured": secured,
            }
    return found


def placeholders(redact: Sequence[str], environ: dict[str, str], supplied: Sequence[str]) -> list[tuple[str, str]]:
    """Each value a cassette may not hold, with what stands in its place; longest first.

    Only the values that a run supplies to the case: another case's value is
    one that the case's replay cannot fill. `NAME=ACCOUNT` redacts the value of
    NAME as `{ACCOUNT}`: a response can name the account that the request named
    by ACCOUNT another way, such as by number, and one placeholder lets replay
    answer with the account the provider sent.
    """
    pairs = []
    if environ.get("TFKIT_RUN_ID"):
        pairs.append((environ["TFKIT_RUN_ID"], "{run_id}"))
    accounts = _accounts(redact, environ)
    pairs += [(value, "{" + account + "}") for value, account in accounts.items()]
    for name in supplied:
        value = environ.get("TFKIT_VAR_" + name.upper(), "")
        if value and value not in accounts:
            pairs.append((value, "{" + name + "}"))
    for value, stand_in in pairs:
        if len(value) < MIN_REPLACED_LENGTH:
            raise CassetteError(
                f"the value that {stand_in} stands for has {len(value)} characters, fewer than "
                f"{MIN_REPLACED_LENGTH}: a value that short also occurs in text that is not it, and "
                "replacing it there breaks the recording. Nothing was sent. Ask the operator for a "
                "longer value"
            )
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def _accounts(redact: Sequence[str], environ: dict[str, str]) -> dict[str, str]:
    """Each `--redact` value, with the name of the placeholder it takes.

    Replay fills an uppercase placeholder from the environment, as the provider
    reads its own settings, and a lowercase one only from the case's values. So
    an account's placeholder is an environment variable, and two variables that
    hold one value take one placeholder: a URL cannot show which one it took.
    """
    found: dict[str, str] = {}
    named: dict[str, str] = {}
    for item in redact:
        name, _, account = item.partition("=")
        account = account or name
        if not ENVIRONMENT_NAME.fullmatch(account):
            raise CassetteError(
                f"--redact {item}: {account} is not an environment variable. Replay fills an account's "
                "placeholder from the environment, as the provider reads its own settings, and a case "
                "value's placeholder from the case. Write the account as the variable that the provider "
                "reads, such as its project setting. Nothing was sent"
            )
        value = environ.get(name)
        if not value:
            continue
        if value in found and found[value] != account:
            raise CassetteError(
                f"{named[value]} and {name} hold one value, so a URL cannot show which one it took. "
                f"Write one as the other, with the variable that the provider reads: --redact "
                f"{name}={found[value]}, or --redact {named[value]}={account}. Nothing was sent"
            )
        found[value], named[value] = account, name
    return found


def supplied_forms(redact: Sequence[str], environ: dict[str, str], supplied: Sequence[str]) -> dict[str, str]:
    """Each value the run supplies to the case that holds the run id or the
    value of a `--redact` variable, written with their placeholders.

    A provider can check the form of such a value, such as a path that the
    read's name pattern accepts, so replay gives the case value this form and
    not a flat stand-in. A value that is an account, such as the project, is
    written as the account's placeholder: no replacement can tell which of
    the two a URL took, and replay then gives both one stand-in. A value with
    neither is not written: it can be a secret.
    """
    pairs = placeholders(redact, environ, ())
    forms = {}
    for name in supplied:
        value = environ.get("TFKIT_VAR_" + name.upper(), "")
        form = scrub(value, pairs)
        if value and form != value:
            forms[name] = form
    return forms


def sensitive(export: dict[str, Any], terraform_type: str) -> set[str]:
    """The name of each attribute of the type, at any depth, that the
    provider's schema export marks sensitive."""
    found: set[str] = set()

    def walk(block: dict[str, Any]) -> None:
        for name, attribute in (block.get("attributes") or {}).items():
            if (attribute or {}).get("sensitive"):
                found.add(str(name))
            walk((attribute or {}).get("nested_type") or {})
        for nested in (block.get("block_types") or {}).values():
            walk((nested or {}).get("block") or {})

    for schema in (export.get("provider_schemas") or {}).values():
        walk((((schema or {}).get("resource_schemas") or {}).get(terraform_type) or {}).get("block") or {})
    return found


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a 30x answer as an HTTPError, because the client copies the
    credential header to the address that the redirect names."""

    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(method: str, url: str, body: bytes | None, headers: dict[str, str], shown: str) -> tuple[int, str, str]:
    """Send the request. A message names it by `shown`, the URL as the cassette holds it."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        # url_problem refuses every scheme but https, and http to a loopback
        # host, before a request reaches this call.
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return response.status, response.headers.get("Content-Type", ""), response.read().decode()
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise CassetteError(
                f"{method} {shown} answered {error.code}, a redirect, and nothing was "
                "recorded: a redirect would send the credential to another address"
            ) from error
        return error.code, error.headers.get("Content-Type", ""), error.read().decode()
    except (urllib.error.URLError, TimeoutError) as error:
        raise CassetteError(f"{method} {shown} reached no API, and nothing was recorded: {error}") from error


def link_problem(link: Any, name: str, names: dict[str, dict[str, Any]], directory: Path) -> str | None:
    """Why a read's `after_request` names no recorded call of a change of its
    own type, or None. Replay counts a request other than a GET in a cassette
    other than a read as a change, so a link to a poll or a read never matches."""
    cassette, _, index = str(link).rpartition(":")
    wanted = "--after-request names <cassette>:<index>, a call of a change of the read's own type"
    if (names.get(name) or {}).get("verb") != "read":
        return f"--after-request links a read, and {name} is no read"
    if (
        not index.isdigit()
        or cassette not in names
        or names[cassette]["verb"] == "read"
        or names[cassette]["type"] != names[name]["type"]
    ):
        return f"{wanted}; {link!r} is not one"
    path = directory / f"{cassette}.yaml"
    interactions = (
        (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("interactions") or [] if path.is_file() else []
    )
    if int(index) >= len(interactions):
        return f"{wanted}; {cassette} holds no interaction {index}"
    if str(((interactions[int(index)] or {}).get("request") or {}).get("method", "")).upper() == "GET":
        return f"{wanted}; interaction {index} of {cassette} is a poll"
    return None


def command_record(args: argparse.Namespace, environ: dict[str, str]) -> int:
    names = expected(Path(args.cases))
    if args.name not in names:
        raise CassetteError(f"no case under {args.cases} expects a cassette named {args.name}")
    if not CASSETTE_NAME_FORM.fullmatch(args.name):
        raise CassetteError(
            f"the cassette name {args.name} is not lowercase letters, digits, - and _, "
            "so it could name a file outside --out. Nothing was sent"
        )
    if not environ.get("TFKIT_RUN_ID"):
        raise CassetteError("TFKIT_RUN_ID is unset; a recording names what it creates by the run id")
    if not RUN_ID_FORM.fullmatch(environ["TFKIT_RUN_ID"]):
        raise CassetteError(
            f"TFKIT_RUN_ID is {environ['TFKIT_RUN_ID']}; a run id is tfkit and 8 lowercase letters or "
            "digits, the form the provider's entry point makes, because each name's room was measured "
            "for it. Nothing was sent"
        )
    for name in names[args.name]["named"]:
        if "/" in environ.get("TFKIT_VAR_" + name.upper(), ""):
            raise CassetteError(
                f"TFKIT_VAR_{name.upper()} holds a path: give the name of the existing resource. Each "
                "test builds the form its case's API takes around the name, and replay fills the "
                "recording with it. Nothing was sent"
            )
    if args.after and args.after not in PHASES:
        raise CassetteError(f"--after takes one of {', '.join(PHASES)}")
    if args.after_request and (refused := link_problem(args.after_request, args.name, names, Path(args.out))):
        raise CassetteError(f"{refused}. Nothing was sent")
    sent, kept, secrets = _headers(args, environ, names[args.name])
    marked = sensitive(json.loads(Path(args.provider_export).read_text(encoding="utf-8")), names[args.name]["type"])
    # An API can echo a credential back; it still never reaches the file.
    pairs = [pair for secret in secrets for pair in credential(secret)] + placeholders(
        args.redact or [], environ, names[args.name]["supplied"]
    )
    recorded_url = scrub(_tokenized(args.url, args.token) if args.token else args.url, pairs)
    refused = url_problem(args.url, pairs, args.token)
    if refused:
        raise CassetteError(f"{refused}. Nothing was sent")
    body = Path(args.body).read_bytes() if args.body else None
    status, content_type, text = _send(args.method.upper(), args.url, body, sent, recorded_url)
    interaction: dict[str, Any] = {
        "request": {
            "method": args.method.upper(),
            "url": recorded_url,
            "headers": kept,
            "body": scrub(body.decode(), pairs) if body else "",
        },
        "response": {"status": status, "content_type": content_type, "body": scrub(text, pairs)},
    }
    _refuse_unrecordable(args, names[args.name]["verb"], interaction, marked, pairs)
    if args.after:
        interaction["after"] = args.after
    if args.after_request:
        interaction["after_request"] = args.after_request
    out = Path(args.out) / f"{args.name}.yaml"
    document = (
        yaml.safe_load(out.read_text(encoding="utf-8"))
        if out.exists()
        else {"api_version": API_VERSION, "kind": KIND, "operation": names[args.name]["operation"], "interactions": []}
    )
    document["interactions"].append(interaction)
    forms = supplied_forms(args.redact or [], environ, names[args.name]["supplied"])
    if forms:
        document["supplied"] = {**(document.get("supplied") or {}), **forms}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(document, sort_keys=False, width=100), encoding="utf-8")
    print(f"{out}: {args.method.upper()} answered {status}", file=sys.stderr)
    return 0


def _headers(
    args: argparse.Namespace, environ: dict[str, str], case: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """The headers to send, the headers to keep, and each credential they carry."""
    asserted = {h.lower(): h for h in case["headers"]}
    sent: dict[str, str] = {"Content-Type": "application/json"} if args.body else {}
    kept: dict[str, str] = {}
    secrets: list[str] = []
    for pair in args.header or []:
        name, _, variable = pair.partition("=")
        if name.lower() not in asserted:
            raise CassetteError(f"{name} is not a header the case asserts for {args.name}")
        sent[name] = environ.get(variable, "")
        kept[asserted[name.lower()]] = "{" + variable + "}"
        if name.lower() in case["secured"] and sent[name]:
            secrets.append(sent[name])
    if args.auth_header:
        if not environ.get(args.auth_env or ""):
            raise CassetteError(f"{args.auth_env} holds no credential for {args.auth_header}")
        sent[args.auth_header] = environ[args.auth_env]
        secrets.insert(0, environ[args.auth_env])
    return sent, kept, secrets


def _tokenized(url: str, parameter: str) -> str:
    """The URL with the value of its one de-duplication token parameter as the
    placeholder. Only that parameter changes: another parameter can hold the
    same text, and replay compares it as recorded."""
    head, mark, query = url.partition("?")
    parts = query.split("&") if mark else []
    found = [i for i, part in enumerate(parts) if urllib.parse.unquote_plus(part.partition("=")[0]) == parameter]
    if len(found) != 1 or not parts[found[0]].partition("=")[2]:
        raise CassetteError(
            f"the URL carries no single {parameter} with a value. Nothing was sent. Send the "
            f"request with one new token in {parameter}, as the provider does"
        )
    parts[found[0]] = parts[found[0]].partition("=")[0] + "=" + DEDUP_TOKEN
    return head + mark + "&".join(parts)


def _refuse_unrecordable(
    args: argparse.Namespace, verb: str, interaction: dict[str, Any], marked: set[str], pairs: list[tuple[str, str]]
) -> None:
    """Refuse an exchange that a committed recording must not hold. The request
    was sent, so what it created is in the account."""
    sent = f"{args.method.upper()} {interaction['request']['url']}"
    status = interaction["response"]["status"]
    # The read after the delete may find nothing: replay serves that not-found.
    if not 200 <= status < 300 and not (verb == "read" and args.after == "delete" and status == 404):
        raise CassetteError(
            f"{sent} answered {status}, and nothing was written: replay would serve the failure as "
            "the API's answer. Send the request again once it can succeed"
        )
    held = secrets_held(
        (interaction["request"]["url"], interaction["request"]["body"], interaction["response"]["body"]), marked, pairs
    )
    if held:
        raise CassetteError(
            f"{sent} holds {', '.join(held)}, and nothing was written: a cassette is a committed file. What the "
            "request created is in the account. Stop, and ask the operator how to record this operation"
        )


def problems(path: Path, names: dict[str, dict[str, Any]], raw: list[tuple[str, str]]) -> list[str]:
    """Every way one cassette breaks the format or leaks what it may not hold."""
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    name = path.stem
    if not isinstance(document, dict) or document.get("kind") != KIND or document.get("api_version") != API_VERSION:
        return [f"{name}: not a cassette (kind: {KIND}, api_version: {API_VERSION})"]
    broken = []
    if name not in names:
        broken.append(f"{name}: no case expects a cassette of this name")
    interactions = document.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        broken.append(f"{name}: holds no interaction")
        interactions = []
    asserted = {h.lower() for h in (names.get(name) or {}).get("headers", [])}
    for index, interaction in enumerate(interactions):
        request = (interaction or {}).get("request") or {}
        response = (interaction or {}).get("response") or {}
        if not request.get("method") or not request.get("url") or "status" not in response:
            broken.append(f"{name}: interaction {index} lacks a method, a URL or a status")
        if (interaction or {}).get("after") not in (None, *PHASES):
            broken.append(f"{name}: interaction {index} follows {interaction.get('after')!r}")
        link = (interaction or {}).get("after_request")
        if link is not None and (refused := link_problem(link, name, names, path.parent)):
            broken.append(f"{name}: interaction {index}: {refused}")
        for header in request.get("headers") or {}:
            if CREDENTIALS.match(header):
                broken.append(f"{name}: interaction {index} holds the credential header {header}")
            elif header.lower() not in asserted:
                broken.append(f"{name}: interaction {index} holds {header}, which the case does not assert")
    # YAML folds a long scalar at a space, so the parsed strings are read too.
    texts = [
        text,
        *(
            str(((i or {}).get(part) or {}).get(key) or "")
            for i in interactions
            for part, key in (("request", "url"), ("request", "body"), ("response", "body"))
        ),
    ]
    if re.search(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", "\n".join(texts)):
        broken.append(f"{name}: holds a bearer token")
    broken += [f"{name}: holds {held}" for held in secrets_held(texts, set(), raw)]
    return broken


def command_check(args: argparse.Namespace, environ: dict[str, str]) -> int:
    names = expected(Path(args.cases))
    files = sorted(Path(args.cassettes).glob("*.yaml"))
    if not files:
        raise CassetteError(f"no cassette under {args.cassettes}")
    secret = credential(environ[args.auth_env]) if args.auth_env and environ.get(args.auth_env) else []
    broken = []
    for path in files:
        # A case value can be a fixed part of another case's URL, and replay fills
        # a placeholder only from the values of the cassette's own case.
        supplied = (names.get(path.stem) or {}).get("supplied", [])
        values = {name: environ.get("TFKIT_VAR_" + name.upper(), "") for name in supplied}
        other = not_recorded(
            yaml.safe_load(path.read_text(encoding="utf-8")),
            supplied_forms(args.redact or [], environ, supplied),
            values,
        )
        for name in other:
            print(
                f"{path.stem}: TFKIT_VAR_{name.upper()} is not the value that the recording supplied, "
                "so the check does not look for it",
                file=sys.stderr,
            )
        raw = placeholders(args.redact or [], environ, [n for n in supplied if n not in other]) + secret
        broken += problems(path, names, raw)
    for line in broken:
        print(line, file=sys.stderr)
    print(f"{len(files)} cassettes, {len(broken)} problems", file=sys.stderr)
    return 1 if broken else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cassette", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="send one request and append the exchange")
    record.add_argument("--cases", required=True, help="the provider's resource cases directory")
    record.add_argument("--out", required=True, help="the cassettes directory")
    record.add_argument(
        "--provider-export", required=True, help="the provider's schema export, which marks each secret field sensitive"
    )
    record.add_argument("--name", required=True, help="the cassette name that the case gives the operation")
    record.add_argument("--method", required=True)
    record.add_argument("--url", required=True)
    record.add_argument("--body", help="a file that holds the request body")
    record.add_argument("--auth-header", help="the header that carries the credential")
    record.add_argument("--auth-env", help="the environment variable that holds the credential")
    record.add_argument("--header", action="append", help="NAME=VARIABLE, a header the case asserts")
    record.add_argument(
        "--redact",
        action="append",
        help="VARIABLE, whose value never lands in the file; VARIABLE=ACCOUNT writes it as {ACCOUNT}",
    )
    record.add_argument("--after", help="for a read: the verb whose result it shows")
    record.add_argument(
        "--after-request",
        help="for a read: <cassette>:<zero-based index> of the call of a change whose result it shows",
    )
    record.add_argument("--token", help="the query parameter that carries the request's de-duplication token")
    check = sub.add_parser("check", help="refuse a cassette that breaks the format or leaks")
    check.add_argument("--cases", required=True, help="the provider's resource cases directory")
    check.add_argument("--cassettes", required=True, help="the cassettes directory")
    check.add_argument("--auth-env", help="the environment variable that held the credential")
    check.add_argument("--redact", action="append", help="VARIABLE or VARIABLE=ACCOUNT, whose value must not appear")
    return parser


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environ = dict(os.environ) if environ is None else environ
    try:
        if args.command == "record":
            return command_record(args, environ)
        return command_check(args, environ)
    except (CassetteError, OSError, yaml.YAMLError, json.JSONDecodeError) as error:
        print(f"cassette: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
