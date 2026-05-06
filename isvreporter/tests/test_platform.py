# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for platform module."""

from pathlib import Path

from isvreporter.platform import (
    BARE_METAL,
    DEFAULT_PLATFORM,
    KUBERNETES,
    SLURM,
    get_platform_from_config,
    is_valid_platform,
    normalize_platform,
)


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write content to config.yaml under tmp_path and return the file path.

    Args:
        tmp_path: Temporary directory where config.yaml is written.
        content: Config content to write.

    Returns:
        Path to the written config.yaml file.
    """
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return path


class TestNormalizePlatform:
    """Tests for normalize_platform function."""

    def test_normalize_kubernetes_aliases(self) -> None:
        """Test that k8s and kubernetes normalize to KUBERNETES."""
        assert normalize_platform("k8s") == KUBERNETES
        assert normalize_platform("kubernetes") == KUBERNETES
        assert normalize_platform("K8S") == KUBERNETES
        assert normalize_platform("KUBERNETES") == KUBERNETES
        assert normalize_platform("K8s") == KUBERNETES

    def test_normalize_slurm(self) -> None:
        """Test that slurm normalizes correctly."""
        assert normalize_platform("slurm") == SLURM
        assert normalize_platform("SLURM") == SLURM
        assert normalize_platform("Slurm") == SLURM

    def test_normalize_bare_metal_aliases(self) -> None:
        """Test that bare metal aliases normalize correctly."""
        assert normalize_platform("bare_metal") == BARE_METAL
        assert normalize_platform("bare-metal") == BARE_METAL
        assert normalize_platform("BARE_METAL") == BARE_METAL
        assert normalize_platform("bm") == BARE_METAL

    def test_normalize_empty_and_none(self) -> None:
        """Test that empty/None returns default platform."""
        assert normalize_platform(None) == DEFAULT_PLATFORM
        assert normalize_platform("") == DEFAULT_PLATFORM

    def test_normalize_with_whitespace(self) -> None:
        """Test that whitespace is stripped."""
        assert normalize_platform("  k8s  ") == KUBERNETES
        assert normalize_platform("\tslurm\n") == SLURM

    def test_normalize_unknown_returns_default(self) -> None:
        """Test that unknown platforms return default."""
        assert normalize_platform("unknown") == DEFAULT_PLATFORM
        assert normalize_platform("docker") == DEFAULT_PLATFORM
        assert normalize_platform("aws") == DEFAULT_PLATFORM


class TestIsValidPlatform:
    """Tests for is_valid_platform function."""

    def test_valid_platforms(self) -> None:
        """Test that known platforms are valid."""
        assert is_valid_platform("kubernetes") is True
        assert is_valid_platform("k8s") is True
        assert is_valid_platform("slurm") is True
        assert is_valid_platform("bare_metal") is True
        assert is_valid_platform("bare-metal") is True
        assert is_valid_platform("bm") is True

    def test_valid_platforms_case_insensitive(self) -> None:
        """Test that platform validation is case insensitive."""
        assert is_valid_platform("KUBERNETES") is True
        assert is_valid_platform("K8S") is True
        assert is_valid_platform("SLURM") is True

    def test_invalid_platforms(self) -> None:
        """Test that unknown platforms are invalid."""
        assert is_valid_platform("unknown") is False
        assert is_valid_platform("docker") is False
        assert is_valid_platform("aws") is False

    def test_empty_and_none_invalid(self) -> None:
        """Test that empty/None are invalid."""
        assert is_valid_platform(None) is False
        assert is_valid_platform("") is False


class TestGetPlatformFromConfig:
    """Tests for get_platform_from_config function."""

    def test_valid_config_with_platform(self, tmp_path: Path) -> None:
        """Test reading platform from valid config."""
        config = _write_config(tmp_path, "tests:\n  platform: slurm\n")
        assert get_platform_from_config(str(config)) == SLURM

    def test_config_with_k8s_alias(self, tmp_path: Path) -> None:
        """Test reading k8s alias from config."""
        config = _write_config(tmp_path, "tests:\n  platform: k8s\n")
        assert get_platform_from_config(str(config)) == KUBERNETES

    def test_config_without_tests_section(self, tmp_path: Path) -> None:
        """Test that missing tests section returns default."""
        config = _write_config(tmp_path, "commands:\n  setup: echo hello\n")
        assert get_platform_from_config(str(config)) == DEFAULT_PLATFORM

    def test_config_without_platform(self, tmp_path: Path) -> None:
        """Test that missing platform field returns default."""
        config = _write_config(tmp_path, "tests:\n  markers: [unit]\n")
        assert get_platform_from_config(str(config)) == DEFAULT_PLATFORM

    def test_nonexistent_file_returns_default(self) -> None:
        """Test that nonexistent file returns default."""
        result = get_platform_from_config("/nonexistent/path/config.yaml")
        assert result == DEFAULT_PLATFORM

    def test_invalid_yaml_returns_default(self, tmp_path: Path) -> None:
        """Test that invalid YAML returns default."""
        config = _write_config(tmp_path, "this is not: valid: yaml: content:\n  - bad")
        assert get_platform_from_config(str(config)) == DEFAULT_PLATFORM

    def test_accepts_path_object(self, tmp_path: Path) -> None:
        """Test that Path objects are accepted."""
        config = _write_config(tmp_path, "tests:\n  platform: kubernetes\n")
        assert get_platform_from_config(config) == KUBERNETES
