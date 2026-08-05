{
  # The LOCAL-INSTALL toolchain (bh-q160.12). NOT the developer toolchain — macOS development
  # stays on mise + Brewfile, and `just bootstrap` is unchanged. This flake exists for the one
  # job mise did badly: making every dependency reachable BY `bh` on a Linux host nobody is
  # sitting at.
  #
  # WHY: `bh` resolves tools with shutil.which() on the inherited PATH (setup.py :: PROBE_TABLE).
  # mise installs into its own tree and only reaches PATH once activated, so tools were
  # structurally invisible — measured on a bare Debian 13 host, `bh setup check` found 2 of 4
  # after a SUCCESSFUL `just bootstrap`. Under this flake it finds 4 of 4. A Nix store path is a
  # real binary on PATH; there is no install-vs-activate gap to fall into.
  #
  # Nix itself is installed BY ROOT in multi-user/daemon mode during phase-1 provisioning, in
  # the same root phase that creates bees:8335 and installs OS packages. Everything below then
  # runs unprivileged.
  description = "beadhive local-install toolchain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # aarch64-darwin is declared so the flake EVALUATES on Apple Silicon, not because macOS
      # local-install is supported — it is explicitly out of scope (bh-q160.12), and macOS
      # development stays on mise.
      #
      # x86_64-darwin is ABSENT deliberately: `nix eval` for it fails with "Nixpkgs 26.11 has
      # dropped support for x86_64-darwin" — Intel Macs are gone from nixpkgs-unstable, so
      # listing it would only ever produce an evaluation error.
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAll = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };

      # `beads` MUST be HEAD, not a tagged release. Every tag through v1.1.2 embeds a dolt
      # older than v2.2.0, whose `bd dolt pull` hangs indefinitely on a large store (upstream
      # beads#4770) — and bh's multi-host sync runs that pull. nixpkgs carries 1.0.3, which is
      # two releases INSIDE that broken range, so the override is not optional.
      #
      # Verified on the built binary: go.mod pins dolt v0.40.5-0.20260715172757-a6690826d767,
      # dated 2026-07-15 — the v2.2.0 era — so this source carries the fix.
      #
      # doInstallCheck = false because nixpkgs' versionCheckHook asserts the `version` string
      # appears in `bd version` output. A HEAD build reports the version baked into its source
      # ("1.1.0 (dev)"), never the rev we label it with, so the check can only ever fail here.
      #
      # RETIREMENT: drop this whole override when a tagged release embeds dolt >= v2.2.0, or
      # when bh-00cq lands and bd talks to an external dolt sql-server, which takes the embedded
      # version out of bd's release cadence entirely. Then plain `pkgs.beads` will do.
      beadsHead = pkgs: pkgs.beads.overrideAttrs (_: {
        version = "HEAD-50763fc";
        src = pkgs.fetchFromGitHub {
          owner = "gastownhall";
          repo = "beads";
          rev = "50763fcba7e87ae54e9e44ca75de1168f201f39b";
          hash = "sha256-k6VsBQAxOfQWYXFctoJf2a3e/gCL0o4nyO+DKqAo6UI=";
        };
        vendorHash = "sha256-J5SiCzn79YoGYd9KYnVmtRqgKoS86mOy99DahJMbO20=";
        doCheck = false;
        doInstallCheck = false;
      });

      # Exactly what setup.py :: PROBE_TABLE requires, plus what `bh` shells out to at runtime.
      # A container runtime is NOT here: it is an operator-supplied prerequisite (bh-q160.1),
      # and native mode needs none at all.
      toolchainFor = pkgs: [
        (beadsHead pkgs)      # bd — PROBE_TABLE
        pkgs.dolt             #      PROBE_TABLE
        pkgs.gh               #      PROBE_TABLE
        pkgs.git-workspace    #      PROBE_TABLE. 1.10.1 prebuilt — the mise/brew routes both
                              #      needed a Rust toolchain plus apt libssl-dev + pkg-config.
        pkgs.git
        pkgs.uv               # installs bh itself
      ];
    in {
      packages = forAll (system:
        let pkgs = pkgsFor system; in {
          beads = beadsHead pkgs;
          default = pkgs.buildEnv {
            name = "beadhive-local-install-toolchain";
            paths = toolchainFor pkgs;
          };
        });

      # `nix develop` for a shell with the toolchain on PATH — how install.sh drives it.
      devShells = forAll (system:
        let pkgs = pkgsFor system; in {
          default = pkgs.mkShell { packages = toolchainFor pkgs; };
        });
    };
}
