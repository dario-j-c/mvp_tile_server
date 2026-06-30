"""Tests for CLI argument parsing and main() entry-point behaviour."""

import os
from unittest.mock import patch

import pytest

from app.__main__ import main, parse_arguments
from tests.conftest import TEST_CONFIG_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(argv):
    """Parse *argv* list, prepending the program name."""
    with patch("sys.argv", ["app"] + argv):
        return parse_arguments()


# ---------------------------------------------------------------------------
# parse_arguments() — individual flags
# ---------------------------------------------------------------------------


def test_defaults():
    args = _parse([])
    assert args.config == "config.json"
    assert args.port == 8000
    assert args.bind == "0.0.0.0"
    assert args.workers == 4
    assert args.reload is False
    assert args.event_mode is False
    assert args.no_scan is False


def test_positional_config():
    assert _parse(["custom.json"]).config == "custom.json"


def test_port_short_flag():
    assert _parse(["-p", "9000"]).port == 9000


def test_port_long_flag():
    assert _parse(["--port", "8080"]).port == 8080


def test_bind_short_flag():
    assert _parse(["-b", "127.0.0.1"]).bind == "127.0.0.1"


def test_bind_long_flag():
    assert _parse(["--bind", "127.0.0.1"]).bind == "127.0.0.1"


def test_workers_flag():
    assert _parse(["--workers", "8"]).workers == 8


def test_reload_flag():
    assert _parse(["--reload"]).reload is True


def test_event_mode_flag():
    assert _parse(["--event-mode"]).event_mode is True


def test_no_scan_flag():
    assert _parse(["--no-scan"]).no_scan is True


# ---------------------------------------------------------------------------
# parse_arguments() — flag combinations
# ---------------------------------------------------------------------------


def test_event_mode_and_no_scan():
    args = _parse(["--event-mode", "--no-scan"])
    assert args.event_mode is True
    assert args.no_scan is True


def test_no_scan_with_workers():
    args = _parse(["--no-scan", "--workers", "8"])
    assert args.no_scan is True
    assert args.workers == 8


def test_custom_config_with_port_and_bind():
    args = _parse(["my.json", "--port", "9090", "--bind", "127.0.0.1"])
    assert args.config == "my.json"
    assert args.port == 9090
    assert args.bind == "127.0.0.1"


def test_all_flags_together():
    args = _parse(
        [
            "my.json",
            "-p",
            "9090",
            "-b",
            "127.0.0.1",
            "--workers",
            "2",
            "--event-mode",
            "--no-scan",
        ]
    )
    assert args.config == "my.json"
    assert args.port == 9090
    assert args.bind == "127.0.0.1"
    assert args.workers == 2
    assert args.event_mode is True
    assert args.no_scan is True
    assert args.reload is False  # default preserved when not specified


def test_reload_with_workers():
    args = _parse(["--reload", "--workers", "1"])
    assert args.reload is True
    assert args.workers == 1


def test_event_mode_no_scan_workers():
    """All three common production flags together."""
    args = _parse(["--event-mode", "--no-scan", "--workers", "8"])
    assert args.event_mode is True
    assert args.no_scan is True
    assert args.workers == 8


def test_config_with_all_optional_flags():
    """Positional config combined with every optional flag."""
    args = _parse(
        [
            "prod.json",
            "--port",
            "80",
            "--bind",
            "0.0.0.0",
            "--workers",
            "16",
            "--event-mode",
            "--no-scan",
        ]
    )
    assert args.config == "prod.json"
    assert args.port == 80
    assert args.workers == 16
    assert args.event_mode is True
    assert args.no_scan is True


# ---------------------------------------------------------------------------
# main() — exit on bad config
# ---------------------------------------------------------------------------


def test_main_invalid_config_exits_1():
    with patch("sys.argv", ["app", "/nonexistent/config.json"]):
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# main() — temp file write failure (no write permission to temp dir)
# ---------------------------------------------------------------------------


def test_main_tempfile_failure_continues_without_metadata_file(caplog):
    """If writing the pre-scan metadata file fails, startup continues and workers scan independently."""
    import logging

    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH)]):
        with patch("uvicorn.run"):
            with patch("app.__main__.scan_all_tilesets", return_value={}):
                with patch(
                    "tempfile.mkstemp", side_effect=OSError("Read-only file system")
                ):
                    with patch.dict("os.environ", {}, clear=False):
                        with caplog.at_level(logging.WARNING):
                            main()  # must not raise or sys.exit

                        # TILE_METADATA_FILE must not be set — workers will scan themselves
                        assert "TILE_METADATA_FILE" not in os.environ

                        # A warning must be logged so the operator knows what happened
                        assert any(
                            "temp file" in r.message.lower()
                            or "metadata" in r.message.lower()
                            for r in caplog.records
                        )


# ---------------------------------------------------------------------------
# main() — env var setup
# ---------------------------------------------------------------------------


def test_main_sets_config_path_env():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run"):
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert os.environ.get("CONFIG_PATH") == str(TEST_CONFIG_PATH)


def test_main_always_sets_tile_scan_to_zero():
    """Workers must never re-scan; MAIN sets TILE_SCAN=0 unconditionally."""
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run"):
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert os.environ.get("TILE_SCAN") == "0"


# ---------------------------------------------------------------------------
# main() — --no-scan behaviour
# ---------------------------------------------------------------------------


def test_main_no_scan_skips_directory_prescan():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run"):
            with patch("app.__main__.scan_all_tilesets") as mock_scan:
                with patch.dict("os.environ", {}, clear=False):
                    main()
                    mock_scan.assert_not_called()


def test_main_no_scan_does_not_set_metadata_file_env():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run"):
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert "TILE_METADATA_FILE" not in os.environ


# ---------------------------------------------------------------------------
# main() — default scan behaviour
# ---------------------------------------------------------------------------


def test_main_default_scan_passes_only_directory_tilesets_to_scan():
    """scan_all_tilesets must receive only directory tilesets, not tar ones."""
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH)]):
        with patch("uvicorn.run"):
            with patch("app.__main__.scan_all_tilesets") as mock_scan:
                mock_scan.return_value = {}
                with patch.dict("os.environ", {}, clear=False):
                    main()
                    mock_scan.assert_called_once()
                    scanned = mock_scan.call_args[0][0]
                    assert all(
                        v["source_type"] == "directory" for v in scanned.values()
                    )


def test_main_default_scan_sets_metadata_file_env():
    """When directory tilesets are present, TILE_METADATA_FILE is set after scan."""
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH)]):
        with patch("uvicorn.run"):
            with patch("app.__main__.scan_all_tilesets", return_value={}):
                with patch.dict("os.environ", {}, clear=False):
                    main()
                    assert "TILE_METADATA_FILE" in os.environ


def test_main_tar_only_config_skips_prescan_even_without_no_scan(tmp_path):
    """If the config has no directory tilesets, scan_all_tilesets is never called."""
    import json
    import tarfile
    from io import BytesIO

    tar_path = tmp_path / "tiles.tar"
    with tarfile.open(tar_path, "w") as tar:
        data = b"fake"
        info = tarfile.TarInfo(name="10/0/0.png")
        info.size = len(data)
        tar.addfile(info, BytesIO(data))

    config = {"tilesets": {"only_tar": str(tar_path)}}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))

    with patch("sys.argv", ["app", str(config_file)]):
        with patch("uvicorn.run"):
            with patch("app.__main__.scan_all_tilesets") as mock_scan:
                with patch.dict("os.environ", {}, clear=False):
                    main()
                    mock_scan.assert_not_called()
                    assert "TILE_METADATA_FILE" not in os.environ


# ---------------------------------------------------------------------------
# main() — uvicorn argument passthrough
# ---------------------------------------------------------------------------


def test_main_event_mode_sets_warning_log_level():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--event-mode", "--no-scan"]):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                kwargs = mock_run.call_args[1]
                assert kwargs.get("log_level") == "warning"
                assert kwargs.get("access_log") is False


def test_main_normal_mode_sets_info_log_level():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                kwargs = mock_run.call_args[1]
                assert kwargs.get("log_level") == "info"
                assert kwargs.get("access_log") is True


def test_main_custom_port_passed_to_uvicorn():
    with patch(
        "sys.argv", ["app", str(TEST_CONFIG_PATH), "--port", "9090", "--no-scan"]
    ):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert mock_run.call_args[1].get("port") == 9090


def test_main_custom_bind_passed_to_uvicorn():
    with patch(
        "sys.argv", ["app", str(TEST_CONFIG_PATH), "--bind", "127.0.0.1", "--no-scan"]
    ):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert mock_run.call_args[1].get("host") == "127.0.0.1"


def test_main_custom_workers_passed_to_uvicorn():
    with patch(
        "sys.argv", ["app", str(TEST_CONFIG_PATH), "--workers", "8", "--no-scan"]
    ):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert mock_run.call_args[1].get("workers") == 8


def test_main_reload_passed_to_uvicorn():
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--reload", "--no-scan"]):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                assert mock_run.call_args[1].get("reload") is True


def test_main_default_port_and_bind_passed_to_uvicorn():
    """Verify defaults survive the round-trip to uvicorn."""
    with patch("sys.argv", ["app", str(TEST_CONFIG_PATH), "--no-scan"]):
        with patch("uvicorn.run") as mock_run:
            with patch.dict("os.environ", {}, clear=False):
                main()
                kwargs = mock_run.call_args[1]
                assert kwargs.get("port") == 8000
                assert kwargs.get("host") == "0.0.0.0"
                assert kwargs.get("workers") == 4
