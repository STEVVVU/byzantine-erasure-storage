import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.compare_read_reuse import run_hyperfine


class HyperfineContractTests(TestCase):
    def test_native_single_run_export_and_complete_cli_command(self):
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "hyperfine.json"
            export = {
                "schema_version": 2,
                "metadata": {"hyperfine_version": "2.0.0"},
                "results": [{"measurements": [{
                    "exit_code": 0,
                    "time_wall_clock": {"value": 0.25, "unit": "second"},
                    "time_cpu": {"value": 0.2, "unit": "second"},
                    "time_user": {"value": 0.15, "unit": "second"},
                    "time_system": {"value": 0.05, "unit": "second"},
                    "memory_peak_resident": {"value": 1048576, "unit": "byte"},
                }]}],
            }

            class Process:
                returncode = 0

                def communicate(self, timeout):
                    self.timeout = timeout
                    raw.write_text(json.dumps(export), encoding="utf-8")
                    return b"", b""

            with patch("scripts.compare_read_reuse.subprocess.Popen", return_value=Process()) as popen:
                result = run_hyperfine(Path("hyperfine"), ["python", "scripts/bfr.py", "read", "f"], raw)

            argv = popen.call_args.args[0]
            self.assertIn("--shell=none", argv)
            self.assertEqual(argv[argv.index("--runs") + 1], "1")
            self.assertEqual(argv[argv.index("--warmup") + 1], "0")
            self.assertEqual(argv[argv.index("--export-json") + 1], str(raw))
            self.assertEqual(argv[argv.index("--metrics") + 1].split(","),
                             ["time_wall_clock", "time_cpu", "time_user", "time_system", "memory_peak_resident"])
            self.assertEqual(result["wall_seconds"], 0.25)
            self.assertEqual(result["json"]["results"][0]["measurements"][0]["exit_code"], 0)
