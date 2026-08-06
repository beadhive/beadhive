{
  # The LOCAL-INSTALL toolchain (bh-q160.12). NOT the developer toolchain — macOS development
  # stays on mise + Brewfile, and `just bootstrap` is unchanged. This flake exists for the one
  # job mise did badly: making every dependency reachable BY `bh` on a Linux host nobody is
  # sitting at.
  #
  # WHY: `bh` resolves tools with shutil.which() on the inherited PATH (deps.py :: DEPS, via
  # setup.probe_one).
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
      #
      # PROVEN STATE, do not assume beyond it: only x86_64-linux has been BUILT and run
      # (beadhive-factory, 2026-08-05 — `bh setup check` 4/4). aarch64-linux and aarch64-darwin
      # EVALUATE and nothing more. aarch64-linux is in scope for local-install and untested
      # only because no arm64 Linux host was available; treat a first run there as unproven.
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

      # Exactly what `bh` requires unconditionally, plus what it shells out to at runtime.
      # The source of truth is `src/beadhive/deps.py` — every row with `required == "always"`;
      # `setup.PROBE_TABLE` is that same derivation, not a second list (bh-hsus.3).
      #
      # DELIBERATELY HAND-MIRRORED, with a test instead of codegen (bh-hsus.2 Q4). Deriving
      # this list works two ways — `builtins.fromJSON (builtins.readFile ./deps.json)` under
      # pure eval, or import-from-derivation — but both trade a hand-mirrored flake for a
      # hand-mirrored generated file plus a codegen step, and the name -> attribute map below
      # stays manual regardless (bd is a HEAD override, not `pkgs.bd`; git, uv and just are not
      # deps.py rows at all). `tests/test_flake_toolchain.py` is the drift gate these comments
      # were only pretending to be.
      #
      # `just` is here because the ENTRY POINT is a just recipe: install.sh (bh-q160.6) runs
      # `nix develop --command just local-install`, and without it that command dies with
      # `exec: just: not found` — measured on beadhive-factory, 2026-08-05, which is the same
      # just-circularity the mise plane hit and solved with `mise exec --`. It is not a bh
      # runtime dependency and so is deliberately not a deps.py row; it is a dependency of the
      # local-install PATH, exactly like git and uv.
      #
      # A container runtime is NOT here: it is an operator-supplied prerequisite (bh-q160.1),
      # and native mode needs none at all. Neither is an agent harness: nixpkgs carries both
      # (claude-code 2.1.220, codex 0.146.0 as of the pinned rev), but claude-code is UNFREE
      # and adding it would make every `nix develop` here fail without `allowUnfree` — which
      # is the same "you accept those terms yourself" line `harness.py` already draws.
      toolchainFor = pkgs: [
        (beadsHead pkgs)      # bd — deps.py, required always
        pkgs.dolt             #      deps.py, required always
        pkgs.gh               #      deps.py, required always
        pkgs.git-workspace    #      deps.py, required always. 1.10.1 prebuilt — the mise/brew
                              #      routes both needed a Rust toolchain plus apt libssl-dev
                              #      + pkg-config.
        pkgs.git
        pkgs.uv               # installs bh itself
        pkgs.just             # runs `local-install`, the entry point itself (bh-q160.5)
      ];
      # THE IMAGE'S SET (bh-8b8o.1), DERIVED from the list above rather than hand-written a second
      # time. The point of nixifying docker/Dockerfile was to stop maintaining one toolchain in two
      # places; a parallel list here would reintroduce exactly that drift one layer down. Two
      # deltas, each with a reason:
      #
      #   -git      GPL-2.0. It reaches the image from the base image's apt, where
      #             tests/test_component_licenses.py scopes it out as "separate programs invoked as
      #             programs", alongside Debian's hundreds of other GPL/LGPL packages. NAMING it
      #             here moves it into "a component we pin", where copyleft is not in ALLOWED and
      #             the gate correctly rejects it. What the transitive CLOSURE drags in is a
      #             different question — answered in docs/ASSURANCE.md (bh-8b8o.2): closure
      #             dependencies are base-layer, only what we NAME is a pinned component.
      #
      #   -uv       already in the image, copied by INDEX DIGEST from the official distroless uv
      #             image — a stronger pin than a nixpkgs version, and one docker/write-manifest.sh
      #             already reports. Taking it from here too would put two uv binaries on PATH with
      #             precedence decided by ordering: the same second-copy-shadows-the-first bug
      #             harness.py hit with npm-beside-native (bh-hsus.1). Caught by listing `bin/` of
      #             the built closure, which had `uv` and `uvx` in it.
      #
      #   +jq       the image's own scripts need them; docker/write-manifest.sh is jq all the way
      #   +yq-go    down. They are NOT deps.py rows and do not belong in `toolchainFor`, which
      #             states what `bh` requires on a HOST — putting them there to save three lines
      #             would make that list mean two things at once.
      #
      # `yq-go` is mikefarah's Go yq, which this repo's scripts are written against. nixpkgs' `yq`
      # is the unrelated Python jq-wrapper with different syntax; picking it would fail at runtime
      # rather than here, which is the worst place for this particular mistake to surface.
      imageToolchainFor = pkgs:
        builtins.filter (p: p != pkgs.git && p != pkgs.uv) (toolchainFor pkgs)
        ++ [ pkgs.jq pkgs.yq-go ];
    in {
      packages = forAll (system:
        let pkgs = pkgsFor system; in {
          beads = beadsHead pkgs;
          default = pkgs.buildEnv {
            name = "beadhive-local-install-toolchain";
            paths = toolchainFor pkgs;
          };
          image = pkgs.buildEnv {
            name = "beadhive-image-toolchain";
            paths = imageToolchainFor pkgs;
          };
        });

      # `nix develop` for a shell with the toolchain on PATH — how install.sh drives it.
      devShells = forAll (system:
        let pkgs = pkgsFor system; in {
          default = pkgs.mkShell { packages = toolchainFor pkgs; };
        });
    };
}
