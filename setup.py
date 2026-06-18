"""Build configuration hook for the iai-mcp wheel.

All project metadata lives in pyproject.toml.  This file exists solely to
register custom setuptools command subclasses that:
- make release sdists self-contained by compiling the TypeScript MCP wrapper
  before the sdist file list is generated;
- stage prebuilt wrapper JS into the wheel when present, falling back to an npm
  build only for source checkouts that do not already contain mcp-wrapper/dist/;
- stage the native-extension type stubs (*.pyi, py.typed) from the Rust
  workspace beside the compiled extension in the wheel.

Wheel staging writes into build_lib, never into the source tree, so an editable
checkout stays clean.  The sdist command is the only command that intentionally
refreshes mcp-wrapper/dist/ so release artifacts and Homebrew source builds do
not need npm network access during the Python wheel build.

Editable installs (pip install -e .) skip the npm build entirely.  The
install script (scripts/install.sh) builds the wrapper separately; the
resolver falls back to mcp-wrapper/dist/ on an editable install.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _OrigBuildPy
from setuptools.command.sdist import sdist as _OrigSdist

_REPO_ROOT = Path(__file__).parent
_WRAPPER_SRC = _REPO_ROOT / "mcp-wrapper"
_WRAPPER_DIST = _WRAPPER_SRC / "dist"

# Tracked type stubs for the native extension; staged flat beside the
# compiled .so in the wheel so that `Path(iai_mcp_native.__file__).parent`
# finds them after installation.
_NATIVE_STUB_SRC = _REPO_ROOT / "rust" / "iai_mcp_native" / "iai_mcp_native"
_NATIVE_STUB_FILES = [
    # Primary stub for the flat-layout wheel (importable as iai_mcp_native.pyi).
    ("__init__.pyi", "iai_mcp_native.pyi"),
    ("embed.pyi", "embed.pyi"),
    ("graph.pyi", "graph.pyi"),
    ("py.typed", "py.typed"),
]


def _require_wrapper_manifest() -> None:
    if not (_WRAPPER_SRC / "package.json").exists():
        raise RuntimeError(
            "mcp-wrapper/package.json not found. The TypeScript wrapper source "
            "must be present to build the MCP wrapper. If you are building from "
            "an sdist, ensure MANIFEST.in includes the mcp-wrapper source."
        )


def _run_npm_wrapper_build() -> None:
    """Install locked npm deps and compile TypeScript into mcp-wrapper/dist/."""
    _require_wrapper_manifest()

    npm_exe = shutil.which("npm")
    if npm_exe is None:
        raise RuntimeError(
            "Node.js/npm is required to build the MCP wrapper from TypeScript. "
            "Install Node.js >=18 and ensure 'npm' is on your PATH, then retry. "
            "Release sdists include prebuilt mcp-wrapper/dist/*.js so downstream "
            "packagers such as Homebrew do not need npm during wheel builds."
        )

    # Install exact locked dependencies.
    subprocess.run(
        [npm_exe, "ci", "--prefer-offline", "--no-audit", "--no-fund"],
        cwd=str(_WRAPPER_SRC),
        check=True,
    )

    # Compile TypeScript → dist/*.js
    subprocess.run(
        [npm_exe, "run", "build"],
        cwd=str(_WRAPPER_SRC),
        check=True,
    )

    _validate_wrapper_dist(_WRAPPER_DIST)


def _validate_wrapper_dist(dist_dir: Path) -> list[Path]:
    if not dist_dir.exists():
        raise RuntimeError(
            f"Expected {dist_dir} to contain prebuilt wrapper JS, but the "
            "directory is absent. Build from a release sdist or run "
            "`cd mcp-wrapper && npm ci && npm run build`."
        )

    js_files = sorted(dist_dir.glob("*.js"))
    if not js_files:
        raise RuntimeError(
            f"No *.js files found in {dist_dir}. The MCP wrapper build produced "
            "no installable output."
        )

    if not (dist_dir / "index.js").exists():
        raise RuntimeError(
            f"Expected {dist_dir / 'index.js'} so the installed package can expose "
            "iai_mcp/_wrapper/index.js."
        )

    return js_files


class _SdistWithWrapper(_OrigSdist):
    """Build precompiled wrapper JS before generating the release sdist list.

    Homebrew builds Python projects from source archives with network access
    disabled except for declared resources.  Including mcp-wrapper/dist/*.js in
    the sdist lets the Python wheel build copy the wrapper into
    iai_mcp/_wrapper/*.js without running npm at formula build time.
    """

    def run(self) -> None:
        # Build before setuptools computes the sdist file list so MANIFEST.in
        # can include the freshly generated mcp-wrapper/dist/*.js files.
        _run_npm_wrapper_build()
        super().run()


class _BuildWithWrapper(_OrigBuildPy):
    """build_py subclass that stages the TS wrapper and native stubs.

    At wheel-build time: collects the package into build_lib (via the parent
    ``build_py``), stages prebuilt JS from mcp-wrapper/dist/ into
    build_lib/iai_mcp/_wrapper/, and stages the native extension type stubs flat
    into build_lib/. If mcp-wrapper/dist/ is absent (for example, a developer
    building directly from a clean checkout), it falls back to
    ``npm ci && npm run build`` inside mcp-wrapper/ first.

    At editable-install time (``pip install -e .``): returns immediately without
    touching npm.  The editable resolver finds the wrapper via mcp-wrapper/dist/
    and the stubs via the maturin editable package directory.
    """

    def run(self) -> None:
        # Editable installs must never trigger npm. The install script builds
        # the wrapper as a separate step.
        if self.editable_mode:
            super().run()
            return

        # Collect the package into build_lib FIRST so build_lib/iai_mcp/ exists,
        # THEN stage the prebuilt or freshly compiled JS into
        # build_lib/iai_mcp/_wrapper/ and the native stubs flat into build_lib/.
        # Staging into build_lib keeps normal wheel builds clean when the sdist
        # already contains mcp-wrapper/dist/*.js.
        super().run()
        self._stage_ts_wrapper()
        self._stage_native_stubs()

    def _stage_ts_wrapper(self) -> None:
        """Stage wrapper JS output into build_lib, building it only if needed."""
        try:
            js_files = _validate_wrapper_dist(_WRAPPER_DIST)
        except RuntimeError:
            _run_npm_wrapper_build()
            js_files = _validate_wrapper_dist(_WRAPPER_DIST)

        wrapper_dest = Path(self.build_lib) / "iai_mcp" / "_wrapper"
        if wrapper_dest.exists():
            shutil.rmtree(wrapper_dest)
        wrapper_dest.mkdir(parents=True)

        # Copy *.js only — source maps (.js.map) are excluded from the wheel.
        for js_file in js_files:
            shutil.copy2(js_file, wrapper_dest / js_file.name)

    def _stage_native_stubs(self) -> None:
        """Stage the native extension type stubs flat into build_lib.

        setuptools-rust places the compiled extension flat at the build_lib
        root (i.e. ``build_lib/iai_mcp_native.cpython-*.so``), so stubs must
        land at the same level to be found via
        ``Path(iai_mcp_native.__file__).parent`` after installation.
        """
        build_lib_root = Path(self.build_lib)
        for src_name, dest_name in _NATIVE_STUB_FILES:
            src = _NATIVE_STUB_SRC / src_name
            if src.exists():
                shutil.copy2(src, build_lib_root / dest_name)


setup(cmdclass={"build_py": _BuildWithWrapper, "sdist": _SdistWithWrapper})
