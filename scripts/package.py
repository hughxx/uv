"""Build a minimal, reproducible submission archive using the standard library."""

from __future__ import annotations

import argparse
import gzip
import io
import os
import tarfile
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = REPOSITORY_ROOT / "CoreGeek"
REQUIRED_FILES = (
    "main3.py",
    "run.sh",
    "pyproject.toml",
    "src/agent/__init__.py",
    "src/agent/application.py",
    "src/agent/protocol.py",
    "src/agent/server.py",
)


def collect_files(project: Path) -> list[tuple[str, bytes]]:
    if project.is_symlink() or not project.is_dir():
        raise ValueError("project must be a real directory")
    project = project.resolve()
    package = project / "src" / "agent"
    for relative in REQUIRED_FILES:
        if not (project / relative).is_file():
            raise ValueError(f"required file missing: {relative}")
    paths = {project / name for name in REQUIRED_FILES}
    for path in package.rglob("*"):
        relative = path.relative_to(project)
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {relative.as_posix()}")
        if path.is_file() and path.suffix == ".py":
            if not any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                paths.add(path)
    files = []
    for path in sorted(paths):
        relative = path.relative_to(project)
        if any(part.is_symlink() for part in (path, *path.parents) if part != project):
            raise ValueError(f"symbolic links are not allowed: {relative.as_posix()}")
        if not path.resolve().is_relative_to(project):
            raise ValueError("source escapes the project directory")
        contents = path.read_bytes()
        if path.suffix in {".py", ".sh", ".toml"}:
            contents = contents.decode("utf-8-sig").replace("\r\n", "\n").encode("utf-8")
        files.append((relative.as_posix(), contents))
    return files


def add_member(archive: tarfile.TarFile, name: str, content: bytes | None = None) -> None:
    member = tarfile.TarInfo(name)
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    if content is None:
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        archive.addfile(member)
    else:
        member.mode = 0o755 if name.endswith("/run.sh") else 0o644
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


def build_archive(project: Path, output: Path, *, overwrite: bool = False) -> Path:
    files = collect_files(project)
    output = output.absolute()
    if output.is_symlink():
        raise ValueError("output must not be a symbolic link")
    if output.suffixes[-2:] != [".tar", ".gz"]:
        raise ValueError("output must end in .tar.gz")
    if output.resolve().is_relative_to(project.resolve()):
        raise ValueError("output must be outside the runtime project")
    if output.exists() and (not overwrite or not output.is_file()):
        raise FileExistsError("output already exists; use --force to replace a regular file")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".coregeek-", delete=False) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    directories = {"CoreGeek"}
                    for name, _ in files:
                        directories.update(
                            f"CoreGeek/{parent.as_posix()}"
                            for parent in Path(name).parents
                            if parent != Path(".")
                        )
                    for directory in sorted(directories):
                        add_member(archive, directory)
                    for name, content in files:
                        add_member(archive, f"CoreGeek/{name}", content)
        if overwrite:
            os.replace(temporary, output)
        else:
            # Same-directory hard link publishes the complete archive atomically
            # and fails if another process already created the output.
            os.link(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Package CoreGeek for competition submission")
    parser.add_argument("--output", type=Path, default=REPOSITORY_ROOT / "dist" / "CoreGeek.tar.gz")
    parser.add_argument("--force", action="store_true", help="replace an existing output archive")
    args = parser.parse_args()
    try:
        output = build_archive(PROJECT_ROOT, args.output, overwrite=args.force)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(1, f"Packaging failed: {error}\n")
    print(f"Created: {output}")
    print("Runtime: CoreGeek/run.sh <port>; baseline strategy, platform validation still required.")


if __name__ == "__main__":
    main()
