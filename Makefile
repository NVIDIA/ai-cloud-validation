MY_ISV_DOMAINS := bare_metal control-plane iam image-registry network observability security vm
DEMO_TARGETS := $(addprefix demo-,$(MY_ISV_DOMAINS))

.PHONY: help pre-commit build test coverage clean lint format install bump-patch bump-fix bump-minor bump-feat bump-major bump bump-check \
	security-trivy security-trivy-detail security-trufflehog ci-security demo-test demo-all $(DEMO_TARGETS) plan plan-coverage

PACKAGES := isvctl isvreporter isvtest
BUMP_SCRIPT := scripts/bump-version.py
RUFF_VERSION := 0.15.12
RUFF := uvx --from ruff==$(RUFF_VERSION) ruff

# DISABLED (2026-03-24): Trivy supply chain compromise - see GHSA-69fq-xp46-6x23.
# Do NOT pull aquasec/trivy images from Docker Hub until Aqua Security regains control.
# When re-enabling, pin by digest and verify against a trusted source (GitHub release, not Docker Hub).
# TRIVY_IMAGE ?= aquasec/trivy@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c  # 0.69.3 (last known-good release)
TRUFFLEHOG_IMAGE ?= trufflesecurity/trufflehog:latest
SECURITY_SKIP_DIRS := .git,dist,htmlcov,.pytest_cache,.ruff_cache,.venv,node_modules,vendor,.terraform
# TRIVY_SARIF ?= vulnerability-scan-results.sarif

help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

pre-commit: ## Run pre-commit on all packages
	@echo "Running pre-commit on all packages..."
	@for pkg in $(PACKAGES); do \
		echo ""; \
		echo "=========================================="; \
		echo "Running pre-commit in $$pkg"; \
		echo "=========================================="; \
		if [ -n "$$GITLAB_CI" ]; then \
			(cd $$pkg && uvx pre-commit run -a --show-diff-on-failure) || exit 1; \
		else \
			(cd $$pkg && uvx pre-commit run -a) || exit 1; \
		fi; \
	done
	@echo ""
	@echo "✅ All pre-commit checks passed!"

lint: ## Run ruff linting on all packages
	@for pkg in $(PACKAGES); do \
		echo "Linting $$pkg..."; \
		(cd $$pkg && $(RUFF) check src/) || exit 1; \
	done

format: ## Format code with ruff on all packages
	@for pkg in $(PACKAGES); do \
		echo "Formatting $$pkg..."; \
		(cd $$pkg && $(RUFF) format src/) || exit 1; \
	done

# DISABLED (2026-03-24): Trivy supply chain compromise - see GHSA-69fq-xp46-6x23.
# security-trivy: ## Trivy fs scan (HIGH/CRITICAL; Docker). Writes $(TRIVY_SARIF), prints per-finding summary; fails on un-ignored findings
# security-trivy-detail: ## List each finding from $(TRIVY_SARIF) (jq)
security-trivy security-trivy-detail:
	@echo "ERROR: Trivy targets are disabled due to active supply chain compromise (GHSA-69fq-xp46-6x23)." >&2
	@echo "See: https://github.com/advisories/GHSA-69fq-xp46-6x23" >&2
	@exit 1

security-trufflehog: ## Run TruffleHog secret scan (Docker; verified/unknown, --only-verified). Exits non-zero if verified secrets are found
	@command -v docker >/dev/null 2>&1 || { echo "docker not found; install Docker to run local security scans"; exit 1; }
	docker run --rm -v "$(CURDIR):/work" -w /work $(TRUFFLEHOG_IMAGE) filesystem /work \
		--results=verified,unknown --only-verified --fail

ci-security: security-trufflehog ## Run local equivalents of CI security scans (Trivy disabled - GHSA-69fq-xp46-6x23)

test: ## Run tests for all packages
	@for pkg in $(PACKAGES); do \
		echo "Testing $$pkg..."; \
		if [ "$$pkg" = "isvtest" ]; then \
			(cd $$pkg && uv run pytest -m unit) || exit 1; \
		else \
			(cd $$pkg && uv run pytest) || exit 1; \
		fi; \
	done
	@echo "Testing scripts..."
	@uv run pytest scripts/tests || exit 1
	@echo ""
	@echo "✅ All tests passed!"

define run_demo
	@echo ""
	@echo "=========================================="
	@echo "Demo test: $(1)"
	@echo "=========================================="
	@echo "Running cmd: ISVCTL_DEMO_MODE=1 uv run isvctl test run -f isvctl/configs/providers/my-isv/config/$(1).yaml"
	@ISVCTL_DEMO_MODE=1 uv run isvctl test run \
		-f isvctl/configs/providers/my-isv/config/$(1).yaml
endef

demo-test: demo-all ## Alias for demo-all (backward compat)

demo-all: ## Run all my-isv living examples (or demo-<domain> for one, e.g. demo-security)
	@echo "Running my-isv living examples in demo mode..."
	@for domain in $(MY_ISV_DOMAINS); do \
		$(MAKE) --no-print-directory demo-$$domain || exit 1; \
	done
	@echo ""
	@echo "✅ All my-isv living examples passed in demo mode!"
	@echo "Domains: $(MY_ISV_DOMAINS)"

$(DEMO_TARGETS): demo-%:
	$(call run_demo,$*)

coverage: ## Run tests with coverage and generate combined report
	@echo "Running tests with coverage..."
	@for pkg in $(PACKAGES); do \
		echo "Testing $$pkg with coverage..."; \
		if [ "$$pkg" = "isvtest" ]; then \
			(cd $$pkg && uv run pytest -m unit --cov=src --cov-report=) || exit 1; \
		else \
			(cd $$pkg && uv run pytest --cov=src --cov-report=) || exit 1; \
		fi; \
	done
	@echo ""
	@echo "Combining coverage reports..."
	@uv run coverage combine $(foreach pkg,$(PACKAGES),$(pkg)/.coverage)
	@uv run coverage xml -o coverage-combined.xml
	@uv run coverage report
	@echo ""
	@echo "✅ Coverage report generated: coverage-combined.xml"

build: ## Build all packages (wheels output to dist/)
	@echo "Building wheel packages..."
	@mkdir -p dist
	@for pkg in $(PACKAGES); do \
		echo "Building $$pkg..."; \
		uv build $$pkg/ --out-dir dist/ --no-build-logs || exit 1; \
	done
	@echo ""
	@echo "✅ All packages built successfully!"
	@echo "Wheels available in dist/"

install: ## Install all packages in development mode
	uv sync
	@echo ""
	@echo "✅ Installation complete!"

bump-patch: ## Bump patch version (e.g. 0.4.2 -> 0.4.3)
	uv run python $(BUMP_SCRIPT) patch

bump-fix: bump-patch ## Alias for bump-patch

bump-minor: ## Bump minor version (e.g. 0.4.2 -> 0.5.0)
	uv run python $(BUMP_SCRIPT) minor

bump-feat: bump-minor ## Alias for bump-minor

bump-major: ## Bump major version (e.g. 0.4.2 -> 1.0.0)
	uv run python $(BUMP_SCRIPT) major

bump: ## Bump to explicit version (e.g. make bump VERSION=1.2.3)
	@if [ -z "$(VERSION)" ]; then echo "Usage: make bump VERSION=x.y.z"; exit 1; fi
	uv run python $(BUMP_SCRIPT) $(VERSION)

bump-check: ## Verify versions match (CI; e.g. make bump-check VERSION=1.2.3)
	@if [ -z "$(VERSION)" ]; then echo "Usage: make bump-check VERSION=x.y.z"; exit 1; fi
	uv run python $(BUMP_SCRIPT) --check $(VERSION)

clean: ## Clean build artifacts and test outputs
	@echo "Cleaning build artifacts and test outputs..."
	@rm -rf dist/
	@rm -f coverage-combined.xml .coverage
	@for pkg in $(PACKAGES); do \
		echo "Cleaning $$pkg..."; \
		rm -rf $$pkg/dist/ $$pkg/.pytest_cache/ $$pkg/__pycache__/; \
		rm -f $$pkg/junit.xml $$pkg/coverage.xml $$pkg/.coverage; \
		find $$pkg -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true; \
		find $$pkg -type f -name "*.pyc" -delete 2>/dev/null || true; \
	done
	@echo "Cleaning scripts..."
	@rm -f scripts/.coverage
	@find scripts -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find scripts -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find scripts -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Clean complete!"

update-spdx-headers: ## Update SPDX headers in all packages
	@echo "Updating SPDX headers in all packages..."
	@uv run python scripts/add_spdx_headers.py
	@echo "✅ SPDX headers updated!"

plan: ## Render docs/test-plan.yaml to AsciiDoc
	@echo "Rendering test plan..."
	@uv run python scripts/test_plan_yaml_to_adoc.py
	@echo "✅ Test plan rendered!"

plan-coverage: ## Report test-plan coverage via class test_ids (CHECK=1 for CI integrity guardrail)
	@uv run python scripts/test_plan_coverage.py $(if $(CHECK),--check,)

.PHONY: changelog-fill
changelog-fill: ## Fill CHANGELOG.md gaps via an LLM CLI (CLI=auto|cursor|codex|claude)
	@scripts/changelog-fill.sh $(CLI)
