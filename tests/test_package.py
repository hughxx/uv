from __future__ import annotations

import http.client
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path, PurePosixPath

from scripts.package import PROJECT_ROOT, REQUIRED_FILES, build_archive


def find_bash() -> str | None:
    candidate = shutil.which("bash")
    if candidate:
        return candidate
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate_path = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate_path.is_file():
                return str(candidate_path)
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            if base:
                candidate_path = Path(base) / "Git" / "bin" / "bash.exe"
                if candidate_path.is_file():
                    return str(candidate_path)
    return None


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coregeek-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "source with spaces" / "CoreGeek"
        shutil.copytree(PROJECT_ROOT, self.project)
        self.output = self.root / "artifact.tar.gz"

    def test_members_metadata_permissions_and_reproducibility(self) -> None:
        (self.project / ".env").write_text("EXCLUDED=1", encoding="utf-8")
        (self.project / "notes.private.md").write_text("excluded", encoding="utf-8")
        (self.project / "src" / "agent" / ".hidden.py").write_text("excluded", encoding="utf-8")
        (self.project / "src" / "agent" / "extension.py").write_text("VALUE = 1\n", encoding="utf-8")
        build_archive(self.project, self.output)
        second = build_archive(self.project, self.root / "second.tar.gz")
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        with tarfile.open(self.output) as archive:
            members = archive.getmembers()
            files = [member for member in members if member.isfile()]
            names = {member.name for member in files}
            self.assertTrue({f"CoreGeek/{name}" for name in REQUIRED_FILES}.issubset(names))
            self.assertIn("CoreGeek/src/agent/extension.py", names)
            self.assertFalse(any(".env" in name or "private" in name or ".hidden" in name for name in names))
            for name in names:
                self.assertTrue(
                    name in {"CoreGeek/main3.py", "CoreGeek/run.sh", "CoreGeek/pyproject.toml"}
                    or (name.startswith("CoreGeek/src/agent/") and name.endswith(".py"))
                )
            for member in members:
                self.assertFalse(member.issym() or member.islnk())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
                self.assertNotIn("..", PurePosixPath(member.name).parts)
                self.assertFalse(PurePosixPath(member.name).is_absolute())
            launcher = archive.getmember("CoreGeek/run.sh")
            self.assertEqual(launcher.mode, 0o755)
            self.assertNotIn(b"\r", archive.extractfile(launcher).read())

    def test_normalizes_launcher_bom_and_crlf(self) -> None:
        launcher = self.project / "run.sh"
        launcher.write_bytes(b"\xef\xbb\xbf" + launcher.read_bytes().replace(b"\n", b"\r\n"))
        build_archive(self.project, self.output)
        with tarfile.open(self.output) as archive:
            contents = archive.extractfile("CoreGeek/run.sh").read()
            self.assertTrue(contents.startswith(b"#!/bin/sh\n"))
            self.assertNotIn(b"\r", contents)

    def test_existing_output_requires_explicit_overwrite(self) -> None:
        self.output.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            build_archive(self.project, self.output)
        self.assertEqual(self.output.read_bytes(), b"keep")
        build_archive(self.project, self.output, overwrite=True)
        self.assertTrue(tarfile.is_tarfile(self.output))

    def test_rejects_missing_entry_and_output_inside_project(self) -> None:
        with self.assertRaises(ValueError):
            build_archive(self.project, self.project / "bad.tar.gz")
        (self.project / "run.sh").unlink()
        with self.assertRaises(ValueError):
            build_archive(self.project, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_symlink_sources(self) -> None:
        link = self.project / "src" / "agent" / "external.py"
        try:
            link.symlink_to(self.project / "main3.py")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {type(error).__name__}")
        with self.assertRaises(ValueError):
            build_archive(self.project, self.output)

    def extract_artifact(self) -> Path:
        build_archive(self.project, self.output)
        extracted = self.root / "unpacked with spaces"
        with tarfile.open(self.output) as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                destination = extracted.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    self.assertTrue(member.isfile())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
                    destination.chmod(member.mode)
        return extracted / "CoreGeek"

    def assert_standalone_server(self, *, shell: bool) -> None:
        bash = find_bash() if shell else None
        if shell and not bash:
            self.skipTest("bash unavailable; Python archive startup still tested")
        project = self.extract_artifact()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["COREGEEK_PYTHON"] = Path(sys.executable).as_posix()
        if shell:
            command = [bash, str(project / "run.sh"), str(port)]
        else:
            command = [sys.executable, "-I", "-B", str(project / "main3.py"), str(port)]
        outside = self.root / "unrelated cwd"
        outside.mkdir()
        process = subprocess.Popen(
            command, cwd=outside, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        events: queue.Queue = queue.Queue()
        stdout_lines, stderr_lines = [], []

        def collect(stream, lines, *, parse_events=False):
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                lines.append(line)
                if parse_events and "{" in line:
                    try:
                        event = json.loads(line[line.index("{"):])
                    except ValueError:
                        continue
                    if event.get("schema") == "coregeek-online-v1":
                        events.put(event)

        readers = [threading.Thread(target=collect, args=(process.stdout, stdout_lines), kwargs={"parse_events": True}, daemon=True),
                   threading.Thread(target=collect, args=(process.stderr, stderr_lines), daemon=True)]
        for reader in readers:
            reader.start()

        def wait_event(name, **matches):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=max(0.001, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if event["event"] == name and all(event.get(key) == value for key, value in matches.items()):
                    # Read the log while the server is still running: catches
                    # wrong stream and pipe buffering, not just exit-time flush.
                    self.assertIsNone(process.poll())
                    return event
            self.fail(f"missing stdout event {name} {matches}; stderr={''.join(stderr_lines)}; stdout={''.join(stdout_lines)}")

        try:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    self.fail(f"packaged process exited before startup: {''.join(stderr_lines)}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail("packaged process did not start within 10 seconds")
                    time.sleep(0.05)
            listening = wait_event("listening")
            self.assertEqual(listening["logStream"], "stdout")
            self.assertRegex(listening["build"], r"^[0-9a-f]{16}$")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("POST", "/", json.dumps({"roundNo": 1}), {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"roleCommandMap": {}, "prompt": "", "executeCmd": ""})
            finally:
                connection.close()
            probe_log = wait_event("turn", status="probe")
            self.assertEqual(probe_log["actionCount"], 0)
            def post(payload):
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    client.request("POST", "/turn", json.dumps(payload), {"Content-Type": "application/json"})
                    reply = client.getresponse()
                    self.assertEqual(reply.status, 200)
                    return json.loads(reply.read())
                finally:
                    client.close()

            # Exercise the actual artifact's strategy and state, not just an
            # idle transport probe. No Demo or local package installation.
            from agent.application import AgentApplication
            from tests.helpers import packet, unit
            sample_path = Path(__file__).resolve().parents[1] / "docs" / "request.txt"
            sample = json.loads(sample_path.read_text(encoding="utf-8-sig"))
            expected = AgentApplication().handle_turn(sample)
            self.assertTrue(expected["roleCommandMap"])
            self.assertEqual(post(sample), expected)
            turn = wait_event("turn", round=sample["roundNo"], status="new")
            self.assertGreater(turn["actionCount"], 0)
            self.assertTrue(turn["responseWritten"])
            self.assertGreater(turn["decision"]["candidates"], 0)
            self.assertIn("buildCheck", turn["decision"])
            self.assertEqual(post(sample), expected)
            cached = wait_event("turn", status="cached")
            self.assertNotIn("selected", cached["decision"])

            task = packet([unit(10011, "pioneer", health=200)], phaseTask="PRIVATE_ARCHIVE_TASK: return the requested text.")
            task_reply = post(task)
            started = wait_event("turn", round=1, status="new_session")
            self.assertGreater(started["promptChars"], 0)
            token = json.loads(task_reply["prompt"])["requestId"]
            answer = "PRIVATE_ARCHIVE_ANSWER"
            task.update(roundNo=2, llmResp=json.dumps({"requestId": token, "kind": "answer", "answer": answer}))
            self.assertEqual(post(task)["roleCommandMap"]["10011"], {"action": "submitAnswer", "taskAnswer": answer})
            submitted = wait_event("turn", round=2)
            self.assertEqual(submitted["actions"]["10011"], {"action": "submitAnswer", "taskAnswerChars": len(answer)})
            night = packet([unit(pos=(4, 5)), unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (6, 6))], round_no=71)
            self.assertEqual(post(night)["roleCommandMap"], {})
            held = wait_event("turn", round=71)
            self.assertEqual(held["decision"]["selected"][0][1], "hold-weapon")
            self.assertEqual(held["decision"]["emptyReason"], "selected_hold")
            night.update(roundNo=72, robot={"roles": [unit(30001, "largeRobot", (3, 10), health=500)]})
            self.assertEqual(post(night)["roleCommandMap"]["10030"]["action"], "attack")
            fired = wait_event("turn", round=72)
            self.assertEqual(fired["decision"]["staffing"]["shotsIssued"], 1)
            self.assertNotIn("PRIVATE_ARCHIVE_", "".join(stdout_lines + stderr_lines))
            self.assertNotIn("coregeek-online-v1", "".join(stderr_lines))
        finally:
            if shell and os.name == "nt" and process.poll() is None:
                # Git Bash may retain a wrapper process on Windows. Stop only
                # this test-owned process tree so no server survives the test.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                if process.poll() is None:
                    process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            for reader in readers:
                reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def test_extracted_archive_starts_without_repository(self) -> None:
        self.assert_standalone_server(shell=False)

    def test_extracted_shell_launcher(self) -> None:
        self.assert_standalone_server(shell=True)


if __name__ == "__main__":
    unittest.main()
