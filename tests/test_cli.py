from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from patch_ledger import cli


def run_cli(*arguments, home=None):
  environment = {
    "PATH": os.environ.get("PATH", ""),
    "PYTHONPATH": str(SRC),
  }
  if home is not None:
    environment["HOME"] = str(home)
  return subprocess.run(
    [sys.executable, "-m", "patch_ledger", *arguments],
    cwd=ROOT,
    env=environment,
    capture_output=True,
    text=True,
    timeout=5,
  )


def run_main(*arguments):
  stdout = StringIO()
  stderr = StringIO()
  with redirect_stdout(stdout), redirect_stderr(stderr):
    return_code = cli.main(arguments)
  return return_code, stdout.getvalue(), stderr.getvalue()


class StageCliTests(unittest.TestCase):
  def test_valid_input_is_staged_under_owned_run_root(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
        home=workspace / "home",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(
        json.loads(result.stdout),
        {
          "destination": "staged/hello.txt",
          "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
          "source": "hello.txt",
          "status": "staged",
        },
      )
      self.assertEqual(
        (run_root / "staged" / "hello.txt").read_text(encoding="utf-8"),
        "hello\n",
      )
      self.assertTrue((run_root / ".patch-ledger-run").is_file())

  def test_tmp_system_alias_can_be_used_as_run_root(self):
    if sys.platform != "darwin":
      self.skipTest("macOS system alias is unavailable")
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(json.loads(result.stdout)["status"], "staged")
      self.assertEqual((run_root / "staged" / "hello.txt").read_text(encoding="utf-8"), "hello\n")

  def test_directory_source_is_rejected_without_traceback_or_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "directory").mkdir()
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "directory"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "source_not_regular_file",
          "path": "directory",
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse(run_root.exists())

  def test_fifo_source_is_rejected_without_blocking_or_mutation(self):
    if not hasattr(os, "mkfifo"):
      self.skipTest("FIFO is unavailable on this platform")
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      os.mkfifo(input_root / "pipe")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "pipe"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "source_not_regular_file",
          "path": "pipe",
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse(run_root.exists())

  def test_malformed_manifest_is_reported_without_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      manifest = workspace / "manifest.json"
      manifest.write_text("not json\n", encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "invalid_manifest",
          "path": str(manifest),
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse(run_root.exists())

  def test_manifest_error_path_redacts_credential_shaped_filename(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      manifest = workspace / ("manifest-" + credential + ".json")
      manifest.write_text("not json\n", encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "invalid_manifest")
      self.assertEqual(payload["error"]["path"], str(manifest).replace(credential, "[redacted]"))
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_owned_run_artifacts_use_owner_only_permissions(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(stat.S_IMODE(run_root.stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE((run_root / ".patch-ledger-run").stat().st_mode), 0o600)
      self.assertEqual(stat.S_IMODE((run_root / "staged" / ".patch-ledger-owned").stat().st_mode), 0o600)

  def test_nested_generated_directories_use_owner_only_permissions(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "nested").mkdir()
      (input_root / "nested" / "deep").mkdir()
      (input_root / "nested" / "deep" / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "nested/deep/hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(stat.S_IMODE((run_root / "staged" / "nested").stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE((run_root / "staged" / "nested" / "deep").stat().st_mode), 0o700)

  def test_absolute_manifest_path_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      source = input_root / "hello.txt"
      source.write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": str(source)}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": str(source),
        },
      })
      self.assertFalse(run_root.exists())

  def test_traversal_manifest_path_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "../hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": "../hello.txt",
        },
      })
      self.assertFalse(run_root.exists())

  def test_reserved_marker_path_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / ".patch-ledger-owned").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": ".patch-ledger-owned"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": ".patch-ledger-owned",
        },
      })
      self.assertFalse(run_root.exists())

  def test_reserved_marker_case_variant_is_rejected_before_run_root_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      source = ".PATCH-LEDGER-owned"
      (input_root / source).write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": source}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": source,
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse(run_root.exists())

  def test_symlink_escape_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      outside_root = workspace / "outside"
      run_root = workspace / "run"
      input_root.mkdir()
      outside_root.mkdir()
      (outside_root / "secret.txt").write_text("not for staging\n", encoding="utf-8")
      (input_root / "link.txt").symlink_to(outside_root / "secret.txt")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "link.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "link.txt",
        },
      })
      self.assertFalse(run_root.exists())

  def test_symlink_loop_is_reported_without_traceback_or_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "loop").symlink_to("loop")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "loop"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "loop",
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse(run_root.exists())

  def test_credential_shaped_input_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      (input_root / "config.txt").write_text(
        "API_TOKEN" + "=" + credential + "\n",
        encoding="utf-8",
      )
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(len(payload["error"]["findings"]), 1)
      finding = payload["error"]["findings"][0]
      self.assertEqual(set(finding), {"category", "fingerprint", "line", "path"})
      self.assertEqual(finding["category"], "github_token")
      self.assertEqual(finding["line"], 1)
      self.assertEqual(finding["path"], "config.txt")
      self.assertRegex(finding["fingerprint"], r"^sha256:[0-9a-f]{12}$")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_credential_shaped_filename_is_redacted_in_error_path(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      filename = "config-" + credential
      (input_root / filename).write_text("API_TOKEN" + "=" + credential + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": filename}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": "config-[redacted]",
        },
      })
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_punctuation_leading_credential_filename_is_redacted(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "!" + "synthetic"
      filename = "config-password=" + credential
      (input_root / filename).write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": filename}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": "[redacted]",
        },
      })
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_quoted_whitespace_credential_filename_is_fully_redacted(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      key = "pass" + "word"
      value = '"' + " secret value" + '"'
      filename = "config-" + key + "=" + value
      (input_root / filename).write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": filename}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": "[redacted]",
        },
      })
      self.assertNotIn(value, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_generic_secret_assignment_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      key = "AWS_" + "SECRET_ACCESS_KEY"
      credential = "synthetic-" + ("a" * 24)
      (input_root / "config.txt").write_text(
        key + "=" + credential + "\n",
        encoding="utf-8",
      )
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(len(payload["error"]["findings"]), 1)
      finding = payload["error"]["findings"][0]
      self.assertEqual(set(finding), {"category", "fingerprint", "line", "path"})
      self.assertEqual(finding["category"], "credential_assignment")
      self.assertEqual(finding["line"], 1)
      self.assertEqual(finding["path"], "config.txt")
      self.assertRegex(finding["fingerprint"], r"^sha256:[0-9a-f]{12}$")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_short_sensitive_assignment_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "secret"
      (input_root / "config.txt").write_text("password" + "=" + credential + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(payload["error"]["findings"][0]["category"], "credential_assignment")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_punctuation_leading_sensitive_assignment_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "!" + "synthetic"
      (input_root / "config.txt").write_text("password" + "=" + credential + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(payload["error"]["findings"][0]["category"], "credential_assignment")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse((run_root / "staged" / "config.txt").exists())

  def test_quoted_assignment_with_leading_whitespace_fails_closed(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      key = "pass" + "word"
      value = '"' + " secret value" + '"'
      (input_root / "config.txt").write_text(key + "=" + value + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(payload["error"]["findings"][0]["category"], "credential_assignment")
      self.assertNotIn(value, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_aws_temporary_access_key_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "ASIA" + "1234567890ABCDEF"
      (input_root / "config.txt").write_text(credential + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(payload["error"]["findings"][0]["category"], "aws_access_key")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_access_key_assignment_fails_closed_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "synthetic-" + ("b" * 24)
      (input_root / "config.txt").write_text("access_key" + "=" + credential + "\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["error"]["category"], "credential_detected")
      self.assertEqual(payload["error"]["findings"][0]["category"], "credential_assignment")
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_non_utf8_credential_shaped_input_fails_closed(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      (input_root / "config.txt").write_bytes(
        ("API_TOKEN" + "=" + credential + "\n").encode("utf-16"),
      )
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "config.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsupported_encoding",
          "path": "config.txt",
        },
      })
      self.assertNotIn(credential, result.stdout + result.stderr)
      self.assertFalse(run_root.exists())

  def test_unmarked_recursive_delete_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      target = run_root / "unmarked"
      target.mkdir(parents=True)
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "unmarked",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unmarked_recursive_delete",
          "path": "unmarked",
        },
      })
      self.assertTrue((target / "keep.txt").is_file())

  def test_clean_rejects_owned_marker_symlink_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      outside_root = workspace / "outside"
      target = run_root / "staged"
      run_root.mkdir()
      outside_root.mkdir()
      target.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker = outside_root / "marker.txt"
      external_marker.write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      (target / ".patch-ledger-owned").symlink_to(external_marker)
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "staged",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "staged/.patch-ledger-owned",
        },
      })
      self.assertTrue((target / "keep.txt").is_file())
      self.assertEqual(external_marker.read_text(encoding="utf-8"), "patch-ledger-owned/v1\n")

  def test_clean_rejects_run_root_symlink_before_external_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_link = workspace / "run-link"
      external_root = workspace / "external"
      target = external_root / "staged"
      external_root.mkdir()
      target.mkdir()
      (external_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      (target / ".patch-ledger-owned").write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")
      run_link.symlink_to(external_root, target_is_directory=True)

      result = run_cli(
        "clean",
        "--run-root",
        str(run_link),
        "--path",
        "staged",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertTrue((target / "keep.txt").is_file())

  def test_stage_rejects_run_root_parent_symlink_before_external_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_parent_link = workspace / "run-parent"
      external_root = workspace / "external"
      run_root = run_parent_link / "run"
      input_root.mkdir()
      external_root.mkdir()
      run_parent_link.symlink_to(external_root, target_is_directory=True)
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertFalse((external_root / "run").exists())

  def test_owned_staging_directory_can_be_cleaned(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      staged = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )
      self.assertEqual(staged.returncode, 0, staged.stderr)

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "staged",
        "--recursive",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(json.loads(result.stdout), {
        "path": "staged",
        "status": "cleaned",
      })
      self.assertFalse((run_root / "staged").exists())

  def test_clean_rmtree_failure_is_reported_as_stable_partial_cleanup_json(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      target = run_root / "staged"
      run_root.mkdir()
      target.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      (target / ".patch-ledger-owned").write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      with patch("patch_ledger.foundation.shutil.rmtree", side_effect=OSError("remove failed")):
        return_code, stdout, stderr = run_main(
          "clean",
          "--run-root",
          str(run_root),
          "--path",
          "staged",
          "--recursive",
        )

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout), {
        "error": {
          "category": "cleanup_failed",
          "limitation": "recursive cleanup is not atomic; partial deletion may have occurred",
          "path": "staged",
        },
      })
      self.assertEqual(stderr, "")
      self.assertTrue((target / "keep.txt").is_file())

  def test_clean_requires_explicit_recursive_confirmation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      staged = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )
      self.assertEqual(staged.returncode, 0, staged.stderr)

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "staged",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "recursive_confirmation_required",
          "path": "staged",
        },
      })
      self.assertTrue((run_root / "staged" / "hello.txt").is_file())

  def test_clean_confirmation_path_redacts_credential_shaped_input(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      path = "delete-" + credential

      result = run_cli(
        "clean",
        "--run-root",
        str(workspace / "run"),
        "--path",
        path,
      )

      self.assertNotEqual(result.returncode, 0)
      payload = json.loads(result.stdout)
      self.assertEqual(payload, {
        "error": {
          "category": "recursive_confirmation_required",
          "path": "delete-[redacted]",
        },
      })
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_clean_rejects_case_variant_reserved_marker_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      target = run_root / ".PATCH-LEDGER-owned"
      run_root.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      target.mkdir()
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        ".PATCH-LEDGER-owned",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": ".PATCH-LEDGER-owned",
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertTrue((target / "keep.txt").is_file())

  def test_clean_confirmation_redacts_punctuation_leading_credential_path(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      credential = "!" + "synthetic"
      path = "delete-password=" + credential

      result = run_cli(
        "clean",
        "--run-root",
        str(workspace / "run"),
        "--path",
        path,
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "recursive_confirmation_required",
          "path": "[redacted]",
        },
      })
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_clean_confirmation_fully_redacts_quoted_whitespace_path(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      key = "pass" + "word"
      value = '"' + " secret value" + '"'
      path = "delete-" + key + "=" + value

      result = run_cli(
        "clean",
        "--run-root",
        str(workspace / "run"),
        "--path",
        path,
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "recursive_confirmation_required",
          "path": "[redacted]",
        },
      })
      self.assertNotIn(value, result.stdout + result.stderr)

  def test_clean_rejects_dot_alias_for_run_root(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      staged = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )
      self.assertEqual(staged.returncode, 0, staged.stderr)

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "./.",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unsafe_path",
          "path": "./.",
        },
      })
      self.assertTrue((run_root / "staged" / "hello.txt").is_file())

  def test_unowned_run_root_is_rejected_before_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      run_root.mkdir()
      (run_root / "keep.txt").write_text("keep\n", encoding="utf-8")
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertTrue((run_root / "keep.txt").is_file())
      self.assertFalse((run_root / "staged").exists())
      self.assertFalse((run_root / ".patch-ledger-run").exists())

  def test_unusable_run_root_is_reported_as_redacted_json_without_traceback(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      blocker = workspace / "not-a-directory"
      run_root = blocker / "run"
      input_root.mkdir()
      blocker.write_text("blocker\n", encoding="utf-8")
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "filesystem_error",
          "path": str(run_root),
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertTrue(blocker.is_file())

  def test_stage_mkdir_oserror_is_reported_as_redacted_json(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      with patch("pathlib.Path.mkdir", side_effect=OSError("mkdir failed")):
        return_code, stdout, stderr = run_main(
          "stage",
          "--input-root",
          str(input_root),
          "--run-root",
          str(run_root),
          "--manifest",
          str(manifest),
        )

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout), {
        "error": {
          "category": "filesystem_error",
          "path": str(run_root),
        },
      })
      self.assertEqual(stderr, "")

  def test_stage_chmod_oserror_is_reported_as_redacted_json(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      with patch("pathlib.Path.chmod", side_effect=OSError("chmod failed")):
        return_code, stdout, stderr = run_main(
          "stage",
          "--input-root",
          str(input_root),
          "--run-root",
          str(run_root),
          "--manifest",
          str(manifest),
        )

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout), {
        "error": {
          "category": "filesystem_error",
          "path": str(run_root),
        },
      })
      self.assertEqual(stderr, "")

  def test_stage_publish_oserror_is_reported_as_redacted_json(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      with patch("patch_ledger.foundation.tempfile.mkstemp", side_effect=OSError("publish failed")):
        return_code, stdout, stderr = run_main(
          "stage",
          "--input-root",
          str(input_root),
          "--run-root",
          str(run_root),
          "--manifest",
          str(manifest),
        )

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout), {
        "error": {
          "category": "filesystem_error",
          "path": str(run_root),
        },
      })
      self.assertEqual(stderr, "")

  def test_invalid_run_marker_is_reported_without_traceback(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      run_root.mkdir()
      (run_root / ".patch-ledger-run").write_bytes(b"\xff\xfe")
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertEqual(result.stderr, "")
      self.assertFalse((run_root / "staged").exists())

  def test_output_symlink_escape_is_rejected_before_external_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      outside_root = workspace / "outside"
      input_root.mkdir()
      run_root.mkdir()
      outside_root.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      (run_root / "staged").symlink_to(outside_root, target_is_directory=True)
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "staged",
        },
      })
      self.assertFalse((outside_root / "hello.txt").exists())
      self.assertFalse((outside_root / ".patch-ledger-owned").exists())

  def test_existing_staged_file_is_not_overwritten(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      source = input_root / "hello.txt"
      source.write_text("first\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      first = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )
      self.assertEqual(first.returncode, 0, first.stderr)
      source.write_text("second\n", encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "destination_exists",
          "path": "staged/hello.txt",
        },
      })
      self.assertEqual(
        (run_root / "staged" / "hello.txt").read_text(encoding="utf-8"),
        "first\n",
      )

  def test_nested_output_symlink_escape_is_rejected_before_external_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      outside_root = workspace / "outside"
      input_root.mkdir()
      run_root.mkdir()
      outside_root.mkdir()
      staged = run_root / "staged"
      staged.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      (staged / ".patch-ledger-owned").write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      (staged / "nested").symlink_to(outside_root, target_is_directory=True)
      (input_root / "nested").mkdir()
      (input_root / "nested" / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "nested/hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "staged/nested",
        },
      })
      self.assertFalse((outside_root / "hello.txt").exists())

  def test_owned_marker_symlink_is_rejected_before_external_mutation(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      outside_root = workspace / "outside"
      input_root.mkdir()
      run_root.mkdir()
      outside_root.mkdir()
      staged = run_root / "staged"
      staged.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker = outside_root / "marker.txt"
      external_marker.write_text("external\n", encoding="utf-8")
      (staged / ".patch-ledger-owned").symlink_to(external_marker)
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "symlink_escape",
          "path": "staged/.patch-ledger-owned",
        },
      })
      self.assertEqual(external_marker.read_text(encoding="utf-8"), "external\n")
      self.assertFalse((run_root / "staged" / "hello.txt").exists())

  def test_clean_rejects_hardlinked_marker_before_deletion(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      target = run_root / "staged"
      run_root.mkdir()
      target.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker = workspace / "external-marker"
      external_marker.write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      os.link(external_marker, target / ".patch-ledger-owned")
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "staged",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unmarked_recursive_delete",
          "path": "staged",
        },
      })
      self.assertTrue((target / "keep.txt").is_file())
      self.assertTrue(external_marker.exists())

  def test_clean_rejects_hardlinked_run_marker_before_deletion(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      run_root = workspace / "run"
      target = run_root / "staged"
      run_root.mkdir()
      target.mkdir()
      external_marker = workspace / "external-marker"
      external_marker.write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker.chmod(0o644)
      os.link(external_marker, run_root / ".patch-ledger-run")
      (target / ".patch-ledger-owned").write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      (target / "keep.txt").write_text("keep\n", encoding="utf-8")

      result = run_cli(
        "clean",
        "--run-root",
        str(run_root),
        "--path",
        "staged",
        "--recursive",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertTrue((target / "keep.txt").is_file())
      self.assertEqual(stat.S_IMODE(external_marker.stat().st_mode), 0o644)

  def test_hardlinked_staged_marker_is_rejected_without_external_chmod(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      staged = run_root / "staged"
      input_root.mkdir()
      run_root.mkdir()
      staged.mkdir()
      (run_root / ".patch-ledger-run").write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker = workspace / "external-marker"
      external_marker.write_text("patch-ledger-owned/v1\n", encoding="utf-8")
      external_marker.chmod(0o644)
      os.link(external_marker, staged / ".patch-ledger-owned")
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_staged_directory",
          "path": "staged",
        },
      })
      self.assertEqual(stat.S_IMODE(external_marker.stat().st_mode), 0o644)
      self.assertFalse((run_root / "staged" / "hello.txt").exists())

  def test_hardlinked_run_marker_is_rejected_without_external_chmod(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      input_root = workspace / "input"
      run_root = workspace / "run"
      input_root.mkdir()
      run_root.mkdir()
      external_marker = workspace / "external-marker"
      external_marker.write_text("patch-ledger-run/v1\n", encoding="utf-8")
      external_marker.chmod(0o644)
      os.link(external_marker, run_root / ".patch-ledger-run")
      (input_root / "hello.txt").write_text("hello\n", encoding="utf-8")
      manifest = workspace / "manifest.json"
      manifest.write_text(json.dumps({"source": "hello.txt"}), encoding="utf-8")

      result = run_cli(
        "stage",
        "--input-root",
        str(input_root),
        "--run-root",
        str(run_root),
        "--manifest",
        str(manifest),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout), {
        "error": {
          "category": "unowned_run_root",
          "path": ".",
        },
      })
      self.assertEqual(stat.S_IMODE(external_marker.stat().st_mode), 0o644)
      self.assertFalse((run_root / "staged").exists())


if __name__ == "__main__":
  unittest.main()
