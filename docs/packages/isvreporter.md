# ISV Reporter

Report validation test results to the ISV Lab Service API.

## Overview

`isvreporter` is a standalone tool for creating and updating test run records via the ISV Lab Service API. It's designed to be used in CI/CD pipelines to report validation test results, but can also be used standalone for manual reporting or integration with other testing frameworks.

## Installation

As part of the ai-cloud-validation workspace:

```bash
uv sync
```

The `isvreporter` command will be available after installation.

## Usage

### Create a Test Run

Typically used in CI/CD `before_script` to initialize a test run:

```bash
isvreporter create \
    --lab-id 3 \
    --platform kubernetes \
    --tags validation-test gitlab-ci \
    --executed-by gitlab-ci \
    --ci-reference "$CI_JOB_URL" \
    --start-time "$CI_JOB_STARTED_AT"
```

This creates a new test run and saves the test run ID to `_output/testrun_id.txt` for later use.

### Update a Test Run

Typically used in CI/CD `after_script` to report completion:

```bash
isvreporter update \
    --lab-id 3 \
    --status SUCCESS \
    --calculate-duration-from "$CI_JOB_STARTED_AT"
```

This reads the test run ID from `_output/testrun_id.txt` and updates it with the final status and duration.

**With Log Output and JUnit XML:**

You can include both the full test execution log and JUnit XML test results:

```bash
isvreporter update \
    --lab-id 3 \
    --status SUCCESS \
    --calculate-duration-from "$CI_JOB_STARTED_AT" \
    --log-file isvtest/pytest-output.log \
    --junit-xml isvtest/junit-validation.xml
```

This command:

- Uploads the JUnit XML test results to the backend (if provided)
- Updates the test run with status, duration, and log output
- Backend parses and processes the test results
- Reads the test run ID from `_output/testrun_id.txt`

The backend supports both single testsuite and multiple testsuites formats (pytest, JUnit, etc.).

**Important:** A test run stays **STARTED** in the portal until an update is sent. If the process that was supposed to call `update` never runs (e.g. job killed, timeout, or create and test run in separate jobs with no update step), the run never reaches SUCCESS or FAILED. See [Troubleshooting: Test runs stuck in STARTED](../guides/troubleshooting-started-tests.md) for causes and fixes.

## Required Environment Variables

- `ISV_SERVICE_ENDPOINT`: API endpoint URL
- `ISV_SSA_ISSUER`: SSA token issuer URL
- `ISV_CLIENT_ID`: OAuth client ID (CI/CD variable)
- `ISV_CLIENT_SECRET`: OAuth client secret (CI/CD variable)

## Features

- **JWT Authentication**: Automatically obtains and uses JWT tokens for API authentication
- **JUnit XML Upload**: Optionally uploads raw JUnit XML for backend parsing and processing
- **Error Handling**: Clear error messages and proper exit codes
- **Automatic Duration Calculation**: Can calculate test duration from start time
- **State Management**: Saves test run ID to `_output/testrun_id.txt` for use across CI script sections
- **Multiple XML Formats**: Supports single testsuite and multiple testsuites formats
- **Single Command**: One `update` command handles status, logs, and test results
- **Type Safe**: Fully typed Python code following modern best practices
- **Modular Design**: Clean separation of concerns (auth, client, CLI)

## Complete CI/CD Example

### Complete Example

```yaml
validation-tests:
  stage: test
  variables:
    ISV_LAB_ID: 3
    ISV_PLATFORM: "kubernetes"
  before_script:
    # Create test run record
    - |
      uv run isvreporter create \
        --lab-id $ISV_LAB_ID \
        --platform "$ISV_PLATFORM" \
        --tags validation-test gitlab-ci \
        --executed-by gitlab-ci \
        --ci-reference "$CI_JOB_URL" \
        --start-time "$CI_JOB_STARTED_AT"
  script:
    # Run tests and capture output (tee shows output AND saves to file)
    - uv --directory=isvtest run pytest -m validation 2>&1 | tee isvtest/pytest-output.log
  after_script:
    # Update test run with status, log, and JUnit XML results
    - |
      uv run isvreporter update \
        --lab-id $ISV_LAB_ID \
        --status "${CI_JOB_STATUS^^}" \
        --calculate-duration-from "$CI_JOB_STARTED_AT" \
        --log-file isvtest/pytest-output.log \
        --junit-xml isvtest/junit-validation.xml
  artifacts:
    reports:
      junit: isvtest/junit-validation.xml
    paths:
      - isvtest/pytest-output.log
```

## GitLab CI/CD Integration

Example job configuration:

```yaml
validation-tests:
  stage: test
  extends: .cds-runner
  variables:
    ISV_LAB_ID: 3
    ISV_PLATFORM: "kubernetes"
  before_script:
    - |
      isvreporter create \
        --lab-id $ISV_LAB_ID \
        --platform "$ISV_PLATFORM" \
        --tags validation-test gitlab-ci \
        --executed-by gitlab-ci \
        --ci-reference "$CI_JOB_URL" \
        --start-time "$CI_JOB_STARTED_AT"
  script:
    - isvtest run validation
  after_script:
    - |
      isvreporter update \
        --lab-id $ISV_LAB_ID \
        --status "${CI_JOB_STATUS^^}" \
        --calculate-duration-from "$CI_JOB_STARTED_AT"
  artifacts:
    reports:
      junit: isvtest/junit-validation.xml
  allow_failure: true
  when: manual
```

## Command Reference

### `isvreporter create`

Create a new test run record.

**Required Arguments:**

- `--lab-id`: Lab ID (integer)
- `--tags`: Space-separated tags for the test run
- `--executed-by`: Who/what executed the test run
- `--ci-reference`: CI job URL or reference
- `--start-time`: Test run start time (ISO 8601 format)

**Optional Arguments:**

- `--config`: Path to isvctl config YAML file (auto-detects platform from `tests.platform`)
- `--platform`: Platform type (kubernetes, slurm, bare_metal). Required only if `--config` is not provided; otherwise auto-detected from config.

**Returns:**

- Saves test run ID to `_output/testrun_id.txt`
- Prints test run details to stdout

### `isvreporter update`

Update an existing test run record.

**Required Arguments:**

- `--lab-id`: Lab ID (integer)
- `--status`: Test run status (SUCCESS, FAILED, etc.)

**Optional Arguments:**

- `--test-run-id`: Test run ID (defaults to reading from `_output/testrun_id.txt`)
- `--duration-seconds`: Test duration in seconds
- `--complete-time`: Test completion time (ISO 8601 format, defaults to now)
- `--calculate-duration-from`: Calculate duration from this start time (ISO 8601 format)
- `--log-file`: Path to log file to include (e.g., pytest output)
- `--junit-xml`: Path to JUnit XML file to upload test results (optional)

## Testing Locally

You can test the tool locally with proper credentials:

```bash
# Export required environment variables
export ISV_SERVICE_ENDPOINT="https://your-isv-lab-service-endpoint"
export ISV_SSA_ISSUER="https://..."
export ISV_CLIENT_ID="your-client-id"
export ISV_CLIENT_SECRET="your-client-secret"

# Create a test run
isvreporter create \
    --lab-id 3 \
    --platform kubernetes \
    --tags manual-test local \
    --executed-by "$(whoami)" \
    --ci-reference "local-test" \
    --start-time "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"

# Update the test run
isvreporter update \
    --lab-id 3 \
    --status SUCCESS \
    --duration-seconds 42
```

## Architecture

The package is organized into three main modules:

- `auth.py`: JWT authentication with SSA
- `client.py`: ISV Lab Service API client (create/update test runs)
- `main.py`: CLI entry point and argument parsing

This modular design makes the code:

- Easy to test
- Easy to maintain
- Reusable in other contexts

## Development

### Running Tests

```bash
cd isvreporter
uv run pytest -v
```

### Linting and Formatting

```bash
uvx ruff check --fix
uvx ruff format
```

### Type Checking

All code includes type annotations and is checked with pyright/mypy.

## License

Copyright (c) 2025 NVIDIA Corporation
