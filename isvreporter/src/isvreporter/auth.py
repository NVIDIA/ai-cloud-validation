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

"""Authentication module for ISV Lab Service API."""

import base64
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_jwt_token(ssa_issuer: str, client_id: str, client_secret: str) -> str:
    """
    Obtain JWT token from SSA using client credentials.

    Args:
        ssa_issuer: SSA token issuer URL
        client_id: OAuth client ID
        client_secret: OAuth client secret

    Returns:
        JWT access token

    Raises:
        SystemExit: If token acquisition fails
    """
    token_url = f"{ssa_issuer}/token"
    data = "scope=create-isv-lab-test-run update-isv-lab-test-run&grant_type=client_credentials"

    # Create basic auth header
    credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}",
    }

    try:
        request = Request(token_url, data=data.encode(), headers=headers, method="POST")
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
            return result["access_token"]
    except HTTPError as e:
        print(f"ERROR: Failed to obtain JWT token (HTTP {e.code})", file=sys.stderr)
        print("       Please check your ISV_CLIENT_ID and ISV_CLIENT_SECRET credentials", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print("ERROR: Failed to obtain JWT token - unable to connect to SSA service", file=sys.stderr)
        print(f"       SSA Issuer: {ssa_issuer}", file=sys.stderr)
        print(f"       Reason: {e.reason}", file=sys.stderr)
        print("       This may be expected in network-restricted environments", file=sys.stderr)
        sys.exit(1)
    except KeyError:
        print("ERROR: Failed to obtain JWT token - unexpected response format", file=sys.stderr)
        print("       The response did not contain an 'access_token' field", file=sys.stderr)
        sys.exit(1)
