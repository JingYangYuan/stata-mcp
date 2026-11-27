import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from stata_mcp_local import cli, config_emit, paths, runtime, source  # noqa: E402
import importlib.util  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402


def _load_jobs_module():
    spec = importlib.util.spec_from_file_location("stata_mcp_jobs", str(ROOT / "bin" / "stata-mcp-jobs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CliAndRuntimeTests(unittest.TestCase):
    def test_write_helper_scripts_includes_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = paths.Layout(tmp)
            layout.ensure()
            written = runtime.write_helper_scripts(layout, ROOT)
            names = {p.name for p in written}
            self.assertEqual(
                names,
                {"stata-mcp-bridge.py", "stata-mcp-start.py", "stata-mcp-jobs.py", "stata_mcp_analysis.py"},
            )
            self.assertTrue((layout.bin_dir / "stata_mcp_analysis.py").is_file())

    def test_config_emit_merge_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "client.json"
            cfg.write_text('{"other": 1, "mcpServers": {"keep": {"command": "x"}}}\n', encoding="utf-8")
            entry = config_emit.server_entry("stdio", "/usr/bin/python3", Path("/tmp/bridge.py"), "127.0.0.1", 4001, Path(tmp))
            status, backup = config_emit.write_config(cfg, "mcpServers", "stata-mcp", entry)
            self.assertEqual(status, "updated")
            self.assertIsNotNone(backup)
            data = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(data["other"], 1)
            self.assertIn("keep", data["mcpServers"])
            self.assertIn("stata-mcp", data["mcpServers"])

    def test_status_json_on_empty_home(self):
        import contextlib
        import io
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        unused_port = probe.getsockname()[1]
        probe.close()

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text(
                json.dumps({"host": "127.0.0.1", "port": unused_port, "engine_port": unused_port + 1}),
                encoding="utf-8",
            )
            parser = cli.build_parser()
            args = parser.parse_args(["status", "--home", tmp, "--json"])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_status(args)
            self.assertEqual(rc, 1)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["runtime"]["healthy"])
            self.assertEqual(payload["runtime"]["port"], unused_port)

    def test_rmtree_force_removes_readonly_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ro_dir = Path(tmp) / "broken_git" / "objects" / "ab"
            ro_dir.mkdir(parents=True)
            ro_file = ro_dir / "packfile.idx"
            ro_file.write_text("readonly", encoding="utf-8")
            ro_file.chmod(stat.S_IREAD)
            self.assertTrue(paths.rmtree_force(Path(tmp) / "broken_git"))
            self.assertFalse((Path(tmp) / "broken_git").exists())

    def test_clone_replaces_corrupted_readonly_dest_atomically(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            upstream = Path(tmp) / "upstream"
            (upstream / "src").mkdir(parents=True)
            (upstream / "src" / "stata_mcp_server.py").write_text("# mock server\n", encoding="utf-8")
            env = {
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
            subprocess.run(["git", "init", str(upstream)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            subprocess.run(["git", "-C", str(upstream), "add", "."], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            subprocess.run(["git", "-C", str(upstream), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

            dest = Path(tmp) / "server"
            broken = dest / ".git" / "objects" / "00"
            broken.mkdir(parents=True)
            ro = broken / "deadbeef"
            ro.write_text("partial", encoding="utf-8")
            ro.chmod(stat.S_IREAD)

            built = source.clone(str(upstream), dest)
            self.assertTrue(built.entry.is_file())
            self.assertEqual(built.origin, "git")
            self.assertEqual(list(Path(tmp).glob("server.staging-*")), [])
            self.assertEqual(list(Path(tmp).glob("server.old-*")), [])


class GatewayJobErrorTests(unittest.TestCase):
    def test_job_consumes_r_error_and_populates_error_diagnosis(self):
        jobs_mod = _load_jobs_module()
        with tempfile.TemporaryDirectory() as tmp:
            do_file = Path(tmp) / "sample.do"
            do_file.write_text(
                "sysuse auto, clear\n"
                "display 1+1\n"
                "regress price missing_var\n",
                encoding="utf-8",
            )
            args = jobs_mod.parse_args(["--home", tmp])
            cfg = jobs_mod.Config(args)
            job = jobs_mod.Job(cfg, "file", str(do_file))
            for line in (
                "Starting execution of sample.do...",
                ". sysuse auto, clear",
                ". display 1+1",
                "2",
                ". regress price missing_var",
                "variable missing_var not found",
                "r(111);",
                "end of do-file",
                "*** Execution completed ***",
            ):
                job._consume(line)
            fatal = jobs_mod.stata_mcp_analysis.detect_stata_error(job.read_records())
            self.assertIsNotNone(fatal)
            job._finish(jobs_mod.FAILED, "Stata %s: %s" % (fatal["error_code"], fatal["error_message"]))
            self.assertEqual(job.status, jobs_mod.FAILED)
            self.assertEqual(job.error_code, "r(111)")
            self.assertEqual(job.error_line, 3)
            self.assertIn("[ERROR LOCATION]", job.result_text())
            self.assertTrue(Path(job.error_path).is_file())


if __name__ == "__main__":
    unittest.main()
