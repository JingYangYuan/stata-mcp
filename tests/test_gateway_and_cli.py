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

    def test_bundled_server_present_and_stages_without_git(self):
        bundled = source.find_bundled(ROOT)
        self.assertIsNotNone(bundled)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "server"
            built = source.stage_bundled(bundled.root, dest)
            self.assertEqual(built.origin, "bundled")
            self.assertTrue(built.entry.is_file())
            self.assertIsNotNone(built.requirements)
            self.assertTrue(built.requirements.is_file())
            self.assertTrue((dest / "LICENSE").is_file())

    def test_unified_server_argv_includes_gateway_home(self):
        self.assertEqual(runtime.DEFAULT_ENGINE_PORT, 4001)
        home_dir = Path("/tmp/home")
        argv = runtime.server_argv(
            "/usr/bin/python3",
            Path("/tmp/server/src/stata_mcp_server.py"),
            "/Applications/StataNow",
            "mp",
            "127.0.0.1",
            4001,
            Path("/tmp/server.log"),
            home=home_dir,
        )
        self.assertIn("--gateway-home", argv)
        self.assertIn(str(home_dir), argv)

    def test_install_parser_accepts_pip_index(self):
        parser = cli.build_parser()
        args = parser.parse_args(["install", "--pip-index", "https://pypi.tuna.tsinghua.edu.cn/simple"])
        self.assertEqual(args.pip_index, "https://pypi.tuna.tsinghua.edu.cn/simple")


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

    def test_gateway_instructions_enforce_five_step_workflow(self):
        jobs_mod = _load_jobs_module()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = jobs_mod.Config(jobs_mod.parse_args(["--home", tmp]))
            try:
                server = jobs_mod.build_server(cfg)
            except ImportError:
                self.skipTest("mcp package not installed in current Python")
            inst = getattr(server, "instructions", "") or ""
            idx1 = inst.find("READ & INSPECT DATA FIRST")
            idx2 = inst.find("CHECK COMMAND AVAILABILITY & READ HELP")
            idx3 = inst.find("WRITE CODE & RUN TO COMPLETE THE TASK")
            idx4 = inst.find("DIAGNOSE ERRORS & RE-RUN ON FAILURE")
            idx5 = inst.find("ELIMINATE LOOPS & ENHANCE READABILITY")
            self.assertTrue(all(i >= 0 for i in (idx1, idx2, idx3, idx4, idx5)), inst)
            self.assertLess(idx1, idx2)
            self.assertLess(idx2, idx3)
            self.assertLess(idx3, idx4)
            self.assertLess(idx4, idx5)


class WindowsRuntimeAndBridgeTests(unittest.TestCase):
    def test_is_alive_handles_invalid_and_self_pid(self):
        import os

        self.assertFalse(runtime.is_alive(0))
        self.assertFalse(runtime.is_alive(-1))
        self.assertTrue(runtime.is_alive(os.getpid()))

    def test_stop_pid_timeout_does_not_assume_sigkill_on_windows(self):
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "test.pid"
            pid_file.write_text("999999\n", encoding="utf-8")
            with mock.patch.object(runtime, "is_alive", return_value=True), \
                 mock.patch.object(runtime.subprocess, "run") as mock_run, \
                 mock.patch.object(runtime.os, "kill", side_effect=AssertionError("os.kill must not be called on nt")) if os.name == "nt" else mock.patch.object(runtime.os, "kill"):
                stopped = runtime._stop_pid(999999, pid_file, timeout=0.05)
                self.assertTrue(stopped)
                self.assertFalse(pid_file.exists())
                if os.name == "nt":
                    self.assertGreaterEqual(mock_run.call_count, 2)

    def test_port_free_detects_bound_loopback_port(self):
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        bound_port = sock.getsockname()[1]
        try:
            self.assertFalse(runtime.port_free("127.0.0.1", bound_port))
        finally:
            sock.close()
        self.assertTrue(runtime.port_free("127.0.0.1", bound_port))

    def test_bridge_default_start_argv_handles_spaces_in_path(self):
        spec = importlib.util.spec_from_file_location("stata_mcp_bridge", str(ROOT / "bin" / "stata-mcp-bridge.py"))
        bridge_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge_mod)

        with tempfile.TemporaryDirectory() as tmp:
            spaced_home = Path(tmp) / "dir with spaces"
            (spaced_home / "bin").mkdir(parents=True)
            launcher = spaced_home / "bin" / "stata-mcp-start.py"
            launcher.write_text("# launcher\n", encoding="utf-8")
            args = bridge_mod.parse_args(["--home", str(spaced_home)])
            settings = bridge_mod.Settings(args)
            self.assertIsInstance(settings.start_argv, list)
            self.assertEqual(settings.start_argv[1], str(launcher))


if __name__ == "__main__":
    unittest.main()
