# frozen_string_literal: true

# Homebrew formula template for iai-mcp.
#
# This file is intentionally kept in-tree as a release template. Before submitting
# it to a tap or homebrew-core, replace the url/sha256 placeholders below and add
# the Python and npm vendored resources required by Homebrew's no-network build
# policy.

class IaiMcp < Formula
  include Language::Python::Virtualenv
  include Language::Node

  desc "MCP server providing persistent memory and learning"
  homepage "https://github.com/CodeAbra/iai-personal-memory-engine"
  url "https://github.com/CodeAbra/iai-personal-memory-engine/archive/refs/tags/v1.1.2.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_ARCHIVE_SHA256"
  license "MIT"

  # iai-mcp requires Python >=3.11,<3.13. Prefer python@3.12 until upstream
  # widens support for Python 3.13+.
  depends_on "python@3.12"

  # Build toolchains used by pyproject.toml and mcp-wrapper/package-lock.json.
  depends_on "cmake" => :build
  depends_on "node" => :build
  depends_on "pkgconf" => :build
  depends_on "rust" => :build

  # Native libraries commonly needed by Python cryptography/scientific packages
  # when Homebrew builds from source resources rather than binary wheels.
  depends_on "openssl@3"

  # TODO: Add Python resources before publishing this formula.
  #
  # Generate/update these with Homebrew tooling from the release tarball, then
  # audit any problematic native dependencies manually:
  #   brew update-python-resources ./packaging/homebrew/iai-mcp.rb
  #
  # pyproject.toml runtime dependencies at v1.1.2 include pyarrow, numpy, scipy,
  # pandas, numba, tiktoken, cryptography, keyring, cachetools, hnswlib, psutil,
  # and setproctitle. Homebrew cannot download them during `brew install`; they
  # must be represented as `resource` blocks or as separate formula dependencies.

  # TODO: Add npm resources before publishing this formula.
  #
  # mcp-wrapper/ has a package-lock.json and should be installed without network
  # access. For a private tap, you may generate npm resource blocks and use
  # Homebrew's Node helper workflow. If all npm tarballs are already vendored by
  # policy in the release archive, `npm ci --offline` may be used instead.
  #
  # Example resource shape for each package-lock entry:
  # resource "@modelcontextprotocol/sdk" do
  #   url "https://registry.npmjs.org/@modelcontextprotocol/sdk/-/sdk-...tgz"
  #   sha256 "..."
  # end

  def install
    ENV["OPENSSL_DIR"] = Formula["openssl@3"].opt_prefix

    # Build the TypeScript/Node MCP wrapper first. `npm ci` is acceptable only
    # when the npm cache/resources have been populated by vendored resource
    # blocks or by vendored artifacts in the release tarball; the build may not
    # reach out to the public npm registry during Homebrew installation.
    cd "mcp-wrapper" do
      if resources.grep(/node|npm|@modelcontextprotocol|typescript|tsx|zod/).any?
        resources.each do |r|
          r.stage buildpath/"npm_cache"/r.name
        end
        system "npm", "ci", "--ignore-scripts", "--cache", buildpath/"npm_cache", "--offline"
      else
        # Template fallback for local release dry-runs. Replace this branch with
        # vendored npm resources before publishing to a Homebrew tap.
        system "npm", "ci", "--ignore-scripts"
      end
      system "npm", "run", "build"
      (libexec/"mcp-wrapper").install "dist", "package.json", "package-lock.json", "node_modules"
    end

    # Build/install the Python package into Homebrew's isolated virtualenv. This
    # uses pyproject.toml (setuptools + setuptools-rust) and installs generated
    # console scripts under libexec/bin.
    virtualenv_install_with_resources

    %w[iai-mcp iai iai-mcp-core].each do |cmd|
      bin.install_symlink libexec/"bin"/cmd
    end
  end

  def caveats
    <<~EOS
      This formula packages iai-mcp's Python CLIs and the Node MCP wrapper.

      Limitations:
        * This template targets Homebrew on macOS. The upstream package includes
          both launchd and systemd deployment assets, but Homebrew services on
          macOS are launchd-based; Linux/systemd service installation should be
          handled by the upstream installer or a Linux package.
        * Before publishing, replace the release archive sha256 and vendor all
          Python/npm resources so the build performs no network access.
        * iai-mcp currently supports Python >=3.11,<3.13, so this formula pins
          Homebrew's python@3.12 until upstream declares Python 3.13 support.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/iai-mcp --version")
    assert_match version.to_s, shell_output("#{bin}/iai --version")
    assert_match version.to_s, shell_output("#{bin}/iai-mcp-core --version")
  end
end
