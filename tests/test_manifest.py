from pathlib import Path

import pytest

from warden.ports import manifest
from warden.ports.manifest import ManifestError

GOOD = """
[project]
name = "shop"

[services.api]
kind = "backend"
preferred_port = 8080

[services.worker]
kind = "worker"

[services.db]
kind = "database"
require_port = 5432
"""


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "warden.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_manifest_names_its_services_after_the_project(tmp_path: Path):
    read = manifest.load(written(tmp_path, GOOD))
    assert read.project == "shop"
    assert [service.name for service in read.services] == ["shop-api", "shop-worker", "shop-db"]


def test_a_service_can_insist_on_its_own_name(tmp_path: Path):
    text = (
        '[project]\nname = "shop"\n\n'
        '[services.api]\nkind = "backend"\nname = "legacy-api"\n'
    )
    read = manifest.load(written(tmp_path, text))
    assert read.services[0].name == "legacy-api"

def test_a_project_without_a_name_keeps_the_bare_keys(tmp_path: Path):
    read = manifest.load(written(tmp_path, '[services.api]\nkind = "backend"\n'))
    assert read.project is None
    assert read.services[0].name == "api"


def test_the_ones_that_can_refuse_the_whole_run_go_first(tmp_path: Path):
    read = manifest.load(written(tmp_path, GOOD))
    assert [service.key for service in read.in_order] == ["db", "api", "worker"]


def test_a_missing_file_says_what_to_do(tmp_path: Path):
    with pytest.raises(ManifestError, match="write one, or say --file"):
        manifest.load(tmp_path / "warden.toml")


def test_a_manifest_with_no_services_is_refused(tmp_path: Path):
    with pytest.raises(ManifestError, match="lists no services"):
        manifest.load(written(tmp_path, '[project]\nname = "shop"\n'))


def test_a_service_without_a_kind_is_refused(tmp_path: Path):
    with pytest.raises(ManifestError, match="needs a kind"):
        manifest.load(written(tmp_path, "[services.api]\n"))


def test_a_misspelt_setting_is_said_out_loud_rather_than_ignored(tmp_path: Path):
    with pytest.raises(ManifestError, match="no setting called prefered_port"):
        manifest.load(written(tmp_path, '[services.api]\nkind = "backend"\nprefered_port = 80\n'))


def test_two_wishes_for_one_port_are_refused(tmp_path: Path):
    text = '[services.api]\nkind = "backend"\npreferred_port = 80\nrequire_port = 81\n'
    with pytest.raises(ManifestError, match="pick one"):
        manifest.load(written(tmp_path, text))


def test_broken_toml_says_so(tmp_path: Path):
    with pytest.raises(ManifestError, match="not readable TOML"):
        manifest.load(written(tmp_path, "[services.api\n"))


def test_a_variable_is_the_project_the_service_and_what_it_holds():
    assert manifest.variable("shop", "api", "port") == "SHOP_API_PORT"
    assert manifest.variable(None, "api", "port") == "API_PORT"
    assert manifest.variable("shop.eu", "web-1", "host") == "SHOP_EU_WEB_1_HOST"


def test_the_env_file_says_it_is_generated(tmp_path: Path):
    read = manifest.load(written(tmp_path, GOOD))
    text = manifest.env_file(
        read,
        {"api": ("127.0.0.1", 8080), "worker": ("127.0.0.1", 8001), "db": ("127.0.0.1", 5432)},
    )
    assert text.splitlines()[0].startswith("# Written by `warden apply`")
    assert "SHOP_API_PORT=8080" in text
    assert "SHOP_DB_HOST=127.0.0.1" in text
    assert text.endswith("\n")
